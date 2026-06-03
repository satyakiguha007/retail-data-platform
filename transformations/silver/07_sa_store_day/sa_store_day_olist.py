# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_store_day` (Olist additive)
# MAGIC
# MAGIC Spine extension that adds Olist store-day rows to the shared `sa_store_day` table.
# MAGIC
# MAGIC All Olist orders aggregate under the single virtual store `STORE = 99999 (OLIST_BR)`.
# MAGIC One `sa_store_day` row per `(STORE=99999, BUSINESS_DATE)` — i.e. one row per calendar
# MAGIC day on which any Olist order settled or was placed.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source** | `retaildp.bronze.olist_orders` |
# MAGIC | **Target** | `retaildp.silver.sa_store_day` (additive — POS bootstraps it) |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_store_day_olist_rejects` |
# MAGIC | **FK** | `silver.sa_store_data` — `STORE = 99999` must exist (pre-flight gate) |
# MAGIC | **Pattern** | Batch read + INSERT-ONLY MERGE |
# MAGIC | **Idempotent** | Yes — deterministic `STORE_DAY_SEQ_NO` + `whenNotMatchedInsertAll` |
# MAGIC | **Streaming** | No — Olist is static (Kaggle, frozen Sep 2016 – Oct 2018) |
# MAGIC
# MAGIC ## Locked decisions
# MAGIC
# MAGIC 1. **`STORE = 99999`** — all Olist orders aggregate under the virtual store `OLIST_BR`.
# MAGIC    The seller dimension (`sa_seller_data`) carries true seller granularity downstream.
# MAGIC
# MAGIC 2. **`BUSINESS_DATE = COALESCE(order_approved_at, order_purchase_timestamp)::date`**.
# MAGIC    Mirrors marketplace's "`BUSINESS_DATE = settle_date`" pattern — the date money cleared.
# MAGIC    Falls back to purchase timestamp for `canceled` / `unavailable` orders that never approved.
# MAGIC    **The SAME expression must appear in `sa_tran_head_olist.py`** for FK alignment.
# MAGIC
# MAGIC 3. **Insert-only MERGE** — Pass-2 lesson, applied verbatim:
# MAGIC    `sa_store_day` is shared across POS / MKT / Olist. Updating telemetry on existing
# MAGIC    rows would risk clobbering whichever channel populated them first. STORE=99999
# MAGIC    doesn't collide with POS stores (33487, 39876, 41203) or MKT stores in practice,
# MAGIC    but the pattern stays consistent. `whenNotMatchedInsertAll` only.
# MAGIC
# MAGIC 4. **`FIRST_TRAN_TS` / `LAST_TRAN_TS`** — min/max of `order_purchase_timestamp`,
# MAGIC    the actual customer wall-clock activity. (`order_approved_at` is settlement-side;
# MAGIC    we want closer-to-register telemetry for parity with POS's `tran_head.tran_datetime`.)
# MAGIC
# MAGIC 5. **`RTLOG_RECORD_COUNT`** — count of distinct `order_id`s per (STORE, BUSINESS_DATE).
# MAGIC    Each Olist order is one bronze row (no fan-out at this layer — items live in
# MAGIC    `olist_order_items`). This count is the natural analogue of POS's RTLOG record count.
# MAGIC
# MAGIC ## DQ rules (failures routed to quarantine)
# MAGIC 1. `BUSINESS_DATE` NOT NULL after the COALESCE (i.e. at least one of approved_at /
# MAGIC    purchase_timestamp was populated). Expected to fail for ~0 Olist rows.
# MAGIC 2. `STORE` found in `silver.sa_store_data` (FK lookup → guards against missed pre-flight).

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, current_timestamp, lit, when, broadcast, coalesce,
    array, array_compact,
    count as f_count, countDistinct, min as f_min, max as f_max,
    xxhash64, dayofmonth, to_date,
)
from pyspark.sql.types import (
    StructType, StructField, LongType, IntegerType, StringType,
    DateType, TimestampType, ArrayType,
)
from delta.tables import DeltaTable

dbutils.widgets.text("source_table", "retaildp.bronze.olist_orders", "Source Bronze Table")
SOURCE_TABLE = dbutils.widgets.get("source_table")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

TARGET_TABLE       = "retaildp.silver.sa_store_day"
QUARANTINE_TABLE   = "retaildp.quarantine.silver_sa_store_day_olist_rejects"
STORE_MASTER_TABLE = "retaildp.silver.sa_store_data"

# Olist virtual store — single store under which all Brazilian e-commerce orders aggregate.
OLIST_STORE = 99999

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pre-flight diagnostics
# MAGIC
# MAGIC Confirm prerequisites before doing any real work. If either of these print fails,
# MAGIC the pre-flight step from the Pass-3 brief was skipped — stop and fix.

# COMMAND ----------

print("=== sa_store_data check (STORE = 99999 must exist) ===")
olist_store_row = (
    spark.table(STORE_MASTER_TABLE)
    .where(col("STORE") == OLIST_STORE)
    .collect()
)
if not olist_store_row:
    raise AssertionError(
        f"FATAL: STORE={OLIST_STORE} not found in {STORE_MASTER_TABLE}. "
        "Run pre-flight step 1: add the OLIST_BR row to stores.csv and re-run 08_sa_store_data.py."
    )
print(f"OK — {olist_store_row[0].asDict()}")

print("\n=== bronze.olist_orders schema ===")
spark.table(SOURCE_TABLE).printSchema()

print("\n=== bronze.olist_orders sample (1 row, vertical) ===")
spark.table(SOURCE_TABLE).limit(1).show(vertical=True, truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema — reused from existing table
# MAGIC
# MAGIC This notebook is an **additive writer**. The target was bootstrapped by
# MAGIC `07_sa_store_day/07_sa_store_day.py` (POS). We don't redefine the schema here —
# MAGIC we read the column list from the live table and project to it. That keeps all
# MAGIC three channel notebooks (POS / MKT / Olist) in lockstep with no duplicated StructType.

# COMMAND ----------

assert spark.catalog.tableExists(TARGET_TABLE), (
    f"{TARGET_TABLE} does not exist. Run 07_sa_store_day/07_sa_store_day.py first "
    "to bootstrap the table; this notebook is an additive writer for the Olist channel."
)

target_schema  = spark.table(TARGET_TABLE).schema
TARGET_COLUMNS = [f.name for f in target_schema.fields]
print(f"Target schema has {len(TARGET_COLUMNS)} columns: {TARGET_COLUMNS}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Batch transform
# MAGIC
# MAGIC One-shot pipeline (Olist is static — no `foreachBatch`, no checkpoint):
# MAGIC
# MAGIC 1. **Read** `bronze.olist_orders`.
# MAGIC 2. **Aggregate** to `(STORE=99999, BUSINESS_DATE)` with `_record_count`, `_first_ts`, `_last_ts`.
# MAGIC 3. **Conform** — derive `STORE_DAY_SEQ_NO`, `DAY`, `AUDIT_STATUS`, project to TARGET_COLUMNS.
# MAGIC 4. **FK validate** — `STORE` exists in `silver.sa_store_data` (the pre-flight guard kicks in here for safety).
# MAGIC 5. **MERGE** — `whenNotMatchedInsertAll` only. No `whenMatchedUpdate`.
# MAGIC 6. **Quarantine** any rejects.

# COMMAND ----------

# 1 + 2. Read + aggregate
bronze_df = spark.table(SOURCE_TABLE)

# BUSINESS_DATE: COALESCE(approved_at, purchase_timestamp) → date.
# Same expression must be used in sa_tran_head_olist.py for FK alignment.
agg = (
    bronze_df
    .withColumn(
        "_business_date",
        to_date(coalesce(col("order_approved_at"), col("order_purchase_timestamp"))),
    )
    .withColumn("_purchase_ts", col("order_purchase_timestamp").cast(TimestampType()))
    .filter(col("_business_date").isNotNull())
    .groupBy("_business_date")
    .agg(
        countDistinct("order_id").alias("_record_count"),
        f_min("_purchase_ts").alias("_first_ts"),
        f_max("_purchase_ts").alias("_last_ts"),
    )
)

# 3. Conformance — virtual STORE, surrogate, derived fields, lineage
conformed = (
    agg
    .select(
        lit(OLIST_STORE).cast(LongType()).alias("STORE"),
        col("_business_date").alias("BUSINESS_DATE"),
        col("_record_count").cast(LongType()).alias("RTLOG_RECORD_COUNT"),
        col("_first_ts").alias("FIRST_TRAN_TS"),
        col("_last_ts").alias("LAST_TRAN_TS"),
    )
    .withColumn("STORE_DAY_SEQ_NO", xxhash64(col("STORE"), col("BUSINESS_DATE")))
    .withColumn("DAY",              dayofmonth(col("BUSINESS_DATE")))
    .withColumn("AUDIT_STATUS",     lit("A"))
    .withColumn("_silver_ts",       current_timestamp())
    .withColumn("_source",          lit(SOURCE_TABLE))
)

print(f"Conformed: {conformed.count()} (STORE, BUSINESS_DATE) candidate rows")

# COMMAND ----------

# 4. FK validation — STORE must exist in silver.sa_store_data
valid_stores = (
    spark.table(STORE_MASTER_TABLE)
    .select(col("STORE").alias("_valid_STORE"))
)

dq = (
    conformed
    .join(broadcast(valid_stores), col("STORE") == col("_valid_STORE"), "left")
    .withColumn(
        "rejection_reason",
        array_compact(array(
            when(col("BUSINESS_DATE").isNull(),
                 lit("BUSINESS_DATE null — both order_approved_at and order_purchase_timestamp were null")),
            when(col("_valid_STORE").isNull(),
                 lit(f"STORE={OLIST_STORE} not found in silver.sa_store_data (FK violation — pre-flight not applied)")),
        )),
    )
    .drop("_valid_STORE")
)

clean   = dq.filter("size(rejection_reason) = 0").drop("rejection_reason")
rejects = dq.filter("size(rejection_reason) > 0").withColumn("_quarantine_ts", current_timestamp())

# Project clean to exact target schema column order
clean = clean.select(*TARGET_COLUMNS)

clean_n, reject_n = clean.count(), rejects.count()
print(f"Clean: {clean_n} rows | Rejects: {reject_n} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## MERGE — insert-only
# MAGIC
# MAGIC Pass-2 locked pattern. If a `(STORE, BUSINESS_DATE)` row already exists from any
# MAGIC other channel, **leave it alone** — don't clobber its telemetry. Olist STORE=99999
# MAGIC is disjoint from POS/MKT in practice, so every clean row should be a fresh insert.

# COMMAND ----------

target = DeltaTable.forName(spark, TARGET_TABLE)
(
    target.alias("t")
    .merge(clean.alias("s"), "t.STORE_DAY_SEQ_NO = s.STORE_DAY_SEQ_NO")
    .whenNotMatchedInsertAll()
    .execute()
)
print("MERGE complete (insert-only).")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quarantine rejects

# COMMAND ----------

if reject_n > 0:
    if not spark.catalog.tableExists(QUARANTINE_TABLE):
        print(f"Creating {QUARANTINE_TABLE}.")
        rejects.write.format("delta").saveAsTable(QUARANTINE_TABLE)
    else:
        print(f"Appending to {QUARANTINE_TABLE}.")
        rejects.write.format("delta").mode("append").saveAsTable(QUARANTINE_TABLE)
else:
    print("No rejects this run.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Validation (Olist-filtered)

# COMMAND ----------

olist_rows = spark.table(TARGET_TABLE).where(col("STORE") == OLIST_STORE)
olist_count = olist_rows.count()
total_count = spark.table(TARGET_TABLE).count()
pos_mkt_count = total_count - olist_count

print(f"silver.sa_store_day Olist row count: {olist_count:,}")
print(f"silver.sa_store_day POS+MKT count:   {pos_mkt_count:,}")
print(f"silver.sa_store_day total:           {total_count:,}")

# 1. PK uniqueness within Olist
dup_count = (
    olist_rows
    .groupBy("STORE_DAY_SEQ_NO")
    .count()
    .where("count > 1")
    .count()
)
assert dup_count == 0, f"PK violation: {dup_count} duplicate STORE_DAY_SEQ_NO within Olist"
print("PK uniqueness check passed (within Olist)")

# 2. Cross-channel disjoint — Olist STORE=99999 must not appear in any other channel's surrogate
olist_seqs = {r.STORE_DAY_SEQ_NO for r in olist_rows.select("STORE_DAY_SEQ_NO").collect()}
non_olist_seqs = {
    r.STORE_DAY_SEQ_NO
    for r in spark.table(TARGET_TABLE).where(col("STORE") != OLIST_STORE).select("STORE_DAY_SEQ_NO").collect()
}
overlap = olist_seqs & non_olist_seqs
assert not overlap, f"Cross-channel collision: {len(overlap)} STORE_DAY_SEQ_NO values shared"
print("Cross-channel disjoint check passed (Olist seqs do not overlap POS/MKT)")

# 3. AUDIT_STATUS domain check (Olist subset)
bad_status = olist_rows.where(~col("AUDIT_STATUS").isin("A", "V", "P")).count()
assert bad_status == 0, f"{bad_status} Olist rows with invalid AUDIT_STATUS"
print("AUDIT_STATUS domain check passed")

# 4. BUSINESS_DATE range — should land inside Sep 2016 – Oct 2018
print("\nOlist BUSINESS_DATE range:")
display(olist_rows.agg(
    f_min("BUSINESS_DATE").alias("min_business_date"),
    f_max("BUSINESS_DATE").alias("max_business_date"),
    f_count("*").alias("days_present"),
))

# 5. Telemetry sanity — RTLOG_RECORD_COUNT distribution
print("\nOlist RTLOG_RECORD_COUNT distribution (first 10 days, descending):")
display(
    olist_rows
    .select("BUSINESS_DATE", "RTLOG_RECORD_COUNT", "FIRST_TRAN_TS", "LAST_TRAN_TS")
    .orderBy(col("RTLOG_RECORD_COUNT").desc())
    .limit(10)
)

# 6. Sample
print("\nSample rows (earliest 5):")
display(olist_rows.orderBy("BUSINESS_DATE").limit(5))
