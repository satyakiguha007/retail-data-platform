# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_store_day` (Marketplace)
# MAGIC
# MAGIC The marketplace sibling of the store-day spine. Reads `bronze.marketplace`, extracts
# MAGIC the distinct `(store_no, settle_date)` pairs, and writes to the same
# MAGIC `silver.sa_store_day` table that POS feeds.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source** | `retaildp.bronze.marketplace` |
# MAGIC | **Target** | `retaildp.silver.sa_store_day` (shared with POS) |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_store_day_marketplace_rejects` (per-source) |
# MAGIC | **FK lookup** | `retaildp.silver.sa_store_data` (broadcast — STORE must exist) |
# MAGIC | **Pattern** | `readStream` + `availableNow` + `foreachBatch` → MERGE |
# MAGIC | **Idempotent** | Yes — `STORE_DAY_SEQ_NO` is deterministic + MERGE on it |
# MAGIC | **Partitioned by** | `BUSINESS_DATE` (inherited from target) |
# MAGIC
# MAGIC ## Why the spine needs marketplace
# MAGIC Pass-1 derived `sa_store_day` from POS only — 3 IND stores × 3 days = 9 rows. The
# MAGIC marketplace bronze spans ~26 stores × ~730 days, and without this notebook ~99.5% of
# MAGIC MKT orders fail the `STORE_DAY_SEQ_NO` FK in `sa_tran_head_marketplace`. Conforming
# MAGIC the spine here is the right architectural fix — every downstream channel (MKT, Olist)
# MAGIC inherits the same conformed store-day registry.
# MAGIC
# MAGIC ## Patterns introduced here (vs `07_sa_store_day.py`)
# MAGIC 1. **Additive writer to the same target** — `STORE_DAY_SEQ_NO` is a channel-neutral
# MAGIC    surrogate (no `RTLOG_ORIG_SYS` in the hash). A `(store, date)` pair that exists in
# MAGIC    both POS and MKT bronze MERGEs to the same row. See pattern #6 for the MERGE strategy.
# MAGIC 2. **Per-source quarantine** — `…silver_sa_store_day_marketplace_rejects` rather than
# MAGIC    sharing the POS quarantine. Same Pass-2 convention as `sa_tran_head_marketplace.py`.
# MAGIC 3. **Per-source checkpoint** — `checkpoints/silver/sa_store_day/marketplace/`. POS keeps
# MAGIC    its own at `checkpoints/silver/sa_store_day/`. The two streams advance independently.
# MAGIC 4. **No bootstrap** — table exists from the POS run. We assert its existence and
# MAGIC    project to the existing column list (`TARGET_COLUMNS`).
# MAGIC 5. **`distinct()` before keying** — one MKT order = one bronze row, but many orders
# MAGIC    share a `(store, settle_date)`. Distinct before the hash collapses ~26,650 orders
# MAGIC    down to ~19,000 store-day rows.
# MAGIC 6. **Custom INSERT-IF-NOT-EXISTS MERGE** — bypasses `_shared/quarantine.merge_and_quarantine`
# MAGIC    for the clean write. The target carries POS-derived telemetry columns
# MAGIC    (`RTLOG_RECORD_COUNT`, `FIRST_TRAN_TS`, `LAST_TRAN_TS`) that MKT has no equivalent for
# MAGIC    — MKT writes NULL into them. A `whenMatchedUpdateAll` strategy would clobber the
# MAGIC    POS-populated values on shared `(STORE, BUSINESS_DATE)` rows. `whenNotMatchedInsertAll`
# MAGIC    only is the correct semantic: MKT is purely additive for store-days POS never saw.
# MAGIC    Quarantine append stays manual (same shape as the helper's reject path).
# MAGIC 7. **POS-derived columns explicitly NULL'd** — `RTLOG_RECORD_COUNT`, `FIRST_TRAN_TS`,
# MAGIC    `LAST_TRAN_TS` cast to the right types (`LongType`, `TimestampType`, `TimestampType`)
# MAGIC    so the projection to `TARGET_COLUMNS` succeeds.
# MAGIC
# MAGIC ## Surrogate formula (MUST match POS notebook)
# MAGIC ```
# MAGIC STORE_DAY_SEQ_NO = xxhash64(STORE, BUSINESS_DATE)
# MAGIC ```
# MAGIC `STORE` is `BIGINT` (LongType), `BUSINESS_DATE` is `DATE` (DateType). The casts are
# MAGIC part of the formula — same as `07_sa_store_day.py`. Drift here means a `(store, date)`
# MAGIC pair would land twice with two different surrogates, breaking the FK from `sa_tran_head`.
# MAGIC Not currently a shared helper; if Pass-3 adds Olist as a third writer, factor this into
# MAGIC `_shared/surrogate_keys.py` as `store_day_seq_no_expr()` and refactor all three.
# MAGIC
# MAGIC ## DQ rules (failures routed to quarantine)
# MAGIC 1. `STORE` NOT NULL and > 0
# MAGIC 2. `BUSINESS_DATE` NOT NULL
# MAGIC 3. `STORE` exists in `silver.sa_store_data` (FK violation if not)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, current_timestamp, lit, when, broadcast,
    array, array_compact,
    xxhash64, dayofmonth,
)
from pyspark.sql.types import (
    StructType, StructField, LongType, IntegerType, StringType,
    DateType, TimestampType, ArrayType,
)
from delta.tables import DeltaTable

dbutils.widgets.text("source_table", "retaildp.bronze.marketplace", "Source Bronze Table")
SOURCE_TABLE = dbutils.widgets.get("source_table")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Shared helpers

# COMMAND ----------

# MAGIC %run ../_shared/quarantine

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

TARGET_TABLE       = "retaildp.silver.sa_store_day"
QUARANTINE_TABLE   = "retaildp.quarantine.silver_sa_store_day_marketplace_rejects"
STORE_MASTER_TABLE = "retaildp.silver.sa_store_data"
CHECKPOINT_PATH    = (
    "abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/silver/sa_store_day/marketplace/"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema — reused from existing table
# MAGIC
# MAGIC This notebook is an **additive writer**. The target table was bootstrapped by
# MAGIC `07_sa_store_day/07_sa_store_day.py` during Pass-1. We don't redefine the schema
# MAGIC here — we read the column list from the existing table at runtime and project to it.

# COMMAND ----------

assert spark.catalog.tableExists(TARGET_TABLE), (
    f"{TARGET_TABLE} does not exist. Run 07_sa_store_day/07_sa_store_day.py first "
    "to bootstrap the table; this notebook is an additive writer for the MKT channel."
)

target_schema  = spark.table(TARGET_TABLE).schema
TARGET_COLUMNS = [f.name for f in target_schema.fields]
print(f"Target schema has {len(TARGET_COLUMNS)} columns: {TARGET_COLUMNS}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## foreachBatch handler
# MAGIC
# MAGIC Per micro-batch:
# MAGIC 1. **Extract + distinct** — one MKT bronze row = one order, but many orders share
# MAGIC    a `(store, settle_date)`. `distinct()` collapses the fan-in before hashing.
# MAGIC 2. **Surrogate** — `xxhash64(STORE, BUSINESS_DATE)`. Must match POS formula exactly.
# MAGIC 3. **Derive** — `DAY = dayofmonth(BUSINESS_DATE)` (ReSA NUMERIC(3)), `AUDIT_STATUS = 'A'`.
# MAGIC 4. **FK enrich** — broadcast join to `sa_store_data`; `_has_store_master` flag.
# MAGIC 5. **Lineage + NULL telemetry** — `_silver_ts`, `_source`, plus explicit NULLs for
# MAGIC    `RTLOG_RECORD_COUNT` / `FIRST_TRAN_TS` / `LAST_TRAN_TS` (POS-derived; no MKT equivalent).
# MAGIC 6. **DQ split** — clean vs reject via `rejection_reason` array.
# MAGIC 7. **Write** — custom `DeltaTable.merge(...).whenNotMatchedInsertAll()` (insert-if-not-exists,
# MAGIC    see pattern #6 above). Quarantine append stays manual.

# COMMAND ----------

def merge_microbatch(microBatchDF: DataFrame, batch_id: int) -> None:
    # 1. Extract (store, business_date) + collapse the order fan-in
    flat = (
        microBatchDF
        .select(
            col("store_no").cast(LongType()).alias("STORE"),
            col("settle_date").cast(DateType()).alias("BUSINESS_DATE"),
        )
        .filter(col("STORE").isNotNull() & col("BUSINESS_DATE").isNotNull())
        .distinct()
    )

    # 2. Surrogate — channel-neutral, MUST match the formula in 07_sa_store_day.py
    keyed = (
        flat
        .withColumn("STORE_DAY_SEQ_NO", xxhash64(col("STORE"), col("BUSINESS_DATE")))
        .withColumn("DAY",              dayofmonth(col("BUSINESS_DATE")).cast(IntegerType()))
        .withColumn("AUDIT_STATUS",     lit("A"))
    )

    # 3. FK enrich — STORE must exist in silver.sa_store_data
    store_master = (
        spark.table(STORE_MASTER_TABLE)
        .select(col("STORE").alias("_sd_STORE"))
        .distinct()
    )
    enriched = (
        keyed
        .join(broadcast(store_master), col("STORE") == col("_sd_STORE"), "left")
        .withColumn("_has_store_master", col("_sd_STORE").isNotNull())
        .drop("_sd_STORE")
    )

    # 4. Lineage + POS-derived telemetry as explicit NULLs.
    # RTLOG_RECORD_COUNT / FIRST_TRAN_TS / LAST_TRAN_TS are aggregations over POS rtlog
    # events — MKT has no equivalent (no per-event timestamps, no rtlog concept). The
    # custom insert-if-not-exists MERGE below guarantees these NULLs never overwrite
    # POS-populated values on shared (STORE, BUSINESS_DATE) rows.
    derived = (
        enriched
        .withColumn("RTLOG_RECORD_COUNT", lit(None).cast(LongType()))
        .withColumn("FIRST_TRAN_TS",      lit(None).cast(TimestampType()))
        .withColumn("LAST_TRAN_TS",       lit(None).cast(TimestampType()))
        .withColumn("_silver_ts", current_timestamp())
        .withColumn("_source",    lit(SOURCE_TABLE))
    )

    # 5. DQ split
    dq = derived.withColumn(
        "rejection_reason",
        array_compact(array(
            when(col("STORE").isNull() | (col("STORE") <= 0),
                 lit("STORE null or non-positive")),
            when(col("BUSINESS_DATE").isNull(),
                 lit("BUSINESS_DATE null")),
            when(~col("_has_store_master"),
                 lit("STORE not in silver.sa_store_data (FK violation)")),
        )),
    )

    clean   = dq.filter("size(rejection_reason) = 0").drop("rejection_reason", "_has_store_master")
    rejects = dq.filter("size(rejection_reason) > 0").drop("_has_store_master")

    # Project clean to exact target schema order
    clean = clean.select(*TARGET_COLUMNS)

    # 6. Custom MERGE — INSERT-IF-NOT-EXISTS semantics.
    # We deliberately do NOT use _shared/quarantine.merge_and_quarantine() here because
    # its `whenMatchedUpdateAll` would clobber POS-populated RTLOG_RECORD_COUNT / FIRST_TRAN_TS
    # / LAST_TRAN_TS with NULL on shared (STORE, BUSINESS_DATE) rows. MKT is purely additive
    # for store-days POS never saw.
    target = DeltaTable.forName(spark, TARGET_TABLE)
    (
        target.alias("t")
        .merge(clean.alias("s"), "t.STORE_DAY_SEQ_NO = s.STORE_DAY_SEQ_NO")
        .whenNotMatchedInsertAll()
        .execute()
    )
    clean_n = clean.count()

    # Quarantine append (manual — same shape as the helper's reject path)
    reject_n = rejects.count()
    if reject_n > 0:
        rejects_with_ts = rejects.withColumn("_quarantine_ts", current_timestamp())
        if not spark.catalog.tableExists(QUARANTINE_TABLE):
            print(f"Creating {QUARANTINE_TABLE}.")
            rejects_with_ts.write.format("delta").saveAsTable(QUARANTINE_TABLE)
        else:
            rejects_with_ts.write.format("delta").mode("append").saveAsTable(QUARANTINE_TABLE)

    print(f"Batch {batch_id}: clean={clean_n} rejects={reject_n}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run the streaming merge

# COMMAND ----------

(
    spark.readStream
    .table(SOURCE_TABLE)
    .writeStream
    .foreachBatch(merge_microbatch)
    .trigger(availableNow=True)
    .option("checkpointLocation", CHECKPOINT_PATH)
    .start()
    .awaitTermination()
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Validation + diagnostics

# COMMAND ----------

total_rows = spark.table(TARGET_TABLE).count()
print(f"silver.sa_store_day total row count: {total_rows:,}")

if spark.catalog.tableExists(QUARANTINE_TABLE):
    q_count = spark.table(QUARANTINE_TABLE).count()
    print(f"MKT quarantine row count:            {q_count:,}")
    if q_count > 0:
        print("\nTop MKT rejection reasons:")
        (
            spark.table(QUARANTINE_TABLE)
            .selectExpr("explode(rejection_reason) as reason")
            .groupBy("reason").count()
            .orderBy(col("count").desc())
            .show(truncate=False)
        )
else:
    print("MKT quarantine row count:            0 (table not created)")

# PK uniqueness on STORE_DAY_SEQ_NO across the whole table (both writers)
dup_count = (
    spark.table(TARGET_TABLE)
    .groupBy("STORE_DAY_SEQ_NO").count()
    .where("count > 1").count()
)
assert dup_count == 0, f"PK violation: {dup_count} duplicate STORE_DAY_SEQ_NO"
print("PK uniqueness check passed (across POS + MKT writers)")

# Surrogate alignment sanity — the 9 POS store-days should still exist with their
# original surrogates. If the MKT formula drifted, we'd see duplicate (STORE, BUSINESS_DATE)
# pairs with different STORE_DAY_SEQ_NO values.
nk_dup = (
    spark.table(TARGET_TABLE)
    .groupBy("STORE", "BUSINESS_DATE").count()
    .where("count > 1").count()
)
assert nk_dup == 0, (
    f"Natural-key duplicate: {nk_dup} (STORE, BUSINESS_DATE) pairs map to multiple "
    "STORE_DAY_SEQ_NO surrogates. MKT formula has drifted from POS."
)
print("Natural-key alignment check passed (MKT formula matches POS)")

# Coverage diagnostics
print("\n=== Store-day count by store (top 30) ===")
spark.table(TARGET_TABLE).groupBy("STORE").count().orderBy(col("count").desc()).show(30)

print("\n=== Date range coverage ===")
spark.table(TARGET_TABLE).selectExpr(
    "min(BUSINESS_DATE)            as first_date",
    "max(BUSINESS_DATE)            as last_date",
    "count(distinct BUSINESS_DATE) as distinct_dates",
    "count(distinct STORE)         as distinct_stores",
).show(truncate=False)
