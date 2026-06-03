# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_tran_head` (Olist additive)
# MAGIC
# MAGIC Third channel writer for the central transactional table. Adds Brazilian
# MAGIC e-commerce orders (`RTLOG_ORIG_SYS='OMS'`) to `sa_tran_head`, bootstrapped by
# MAGIC `sa_tran_head_pos.py` and extended by `sa_tran_head_marketplace.py`.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source (head)** | `retaildp.bronze.olist_orders` |
# MAGIC | **Source (items)** | `retaildp.bronze.olist_order_items` (for VALUE + VENDOR_NO derivation) |
# MAGIC | **Target** | `retaildp.silver.sa_tran_head` (additive — POS bootstraps it) |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_tran_head_olist_rejects` |
# MAGIC | **FK lookups** | `silver.sa_store_day`, `silver.sa_store_data`, `bronze.fx_rates` |
# MAGIC | **Pattern** | Batch JOIN + aggregate → MERGE |
# MAGIC | **Idempotent** | Yes — deterministic `TRAN_SEQ_NO` + MERGE on PK |
# MAGIC | **Streaming** | No — Olist is static (Kaggle, frozen Sep 2016 – Oct 2018) |
# MAGIC
# MAGIC ## What's new in this notebook (vs POS/MKT siblings)
# MAGIC
# MAGIC **First JOIN-aggregate head.** POS and MKT each materialise `VALUE` directly from one
# MAGIC bronze row per transaction. Olist has no order-level VALUE in `olist_orders` — it
# MAGIC must be rolled up from `olist_order_items.price + freight_value`. Also, `VENDOR_NO`
# MAGIC uses the brief's "primary-seller" heuristic (seller_id where order_item_id=1), which
# MAGIC requires a second item-side projection. So this notebook does two child-side
# MAGIC aggregations before joining back to the order.
# MAGIC
# MAGIC ## Locked decisions
# MAGIC
# MAGIC | # | Decision | Rationale |
# MAGIC |---|---|---|
# MAGIC | 1 | `BUSINESS_DATE = COALESCE(order_approved_at, order_purchase_timestamp)::date` | Same expression as `sa_store_day_olist.py` — FK alignment to the spine |
# MAGIC | 2 | `TRAN_DATETIME = order_purchase_timestamp` (wall-clock) | Tie-breaker for the surrogate hash; closer to POS's `tran_head.tran_datetime` semantics than approval ts |
# MAGIC | 3 | `TRAN_SEQ_NO_NATURAL = order_id` | 32-char hex hash, globally unique in Olist (PK in bronze) |
# MAGIC | 4 | `REGISTER = 'OLIST'` (sentinel) | Channel-specific (vs MKT's `'ECOM'`); satisfies NOT NULL constraint |
# MAGIC | 5 | `TRAN_NO = xxhash64(order_id)` | Schema requires LongType NOT NULL; order_id is a string hash, deterministic re-hash is the cleanest deriviation |
# MAGIC | 6 | `VENDOR_NO = seller_id` of `order_item_id=1` | Primary-seller heuristic; ~85% of Olist orders are single-seller. Orders with no items → DQ reject |
# MAGIC | 7 | `TRAN_TYPE`: SALE if status ∈ {delivered, shipped, processing, approved, created, invoiced}; CREFUND if status ∈ {canceled, unavailable} | Olist has no RETURN-equivalent status |
# MAGIC | 8 | `SUB_TRAN_TYPE = UPPER(order_status)` | Full status string; silver doesn't enforce ReSA's char(6) limit |
# MAGIC | 9 | `VALUE = SUM(price + freight_value)` over items | Reconciles cleanly when `sa_tran_item_olist.py` adds the freight dummy line |
# MAGIC | 10 | `TAX_MODE = IGTAX` for COUNTRY='BRA' | Brazil's ICMS is VAT-like, analogous to India's GST |
# MAGIC | 11 | `REF_NO1 = 'OLIST_BR'`, `REF_NO2 = customer_id` | Parallel to MKT's `(marketplace, customer_id)` convention |
# MAGIC
# MAGIC ## DQ rules (failures routed to quarantine)
# MAGIC 1. `TRAN_DATETIME` NOT NULL (parses from `order_purchase_timestamp`)
# MAGIC 2. `BUSINESS_DATE` NOT NULL after COALESCE
# MAGIC 3. `TRAN_TYPE` ∈ `{SALE, CREFUND}` (status mapping succeeded)
# MAGIC 4. `VALUE` NOT NULL (items rolled up — orders with no items fail here)
# MAGIC 5. **`VENDOR_NO` NOT NULL** — orders with no items (no primary seller) quarantine here
# MAGIC 6. `STORE_DAY_SEQ_NO` FK lookup succeeds against `sa_store_day` (Olist spine done first)
# MAGIC 7. `CURRENCY_CODE` FK lookup succeeds against `sa_store_data` (STORE=99999 pre-flight)
# MAGIC 8. `TAX_MODE` derivable from COUNTRY (BRA → IGTAX)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, current_timestamp, lit, when, broadcast, coalesce, upper,
    array, array_compact,
    sum as f_sum, xxhash64, to_date,
)
from pyspark.sql.types import (
    StructType, StructField, LongType, IntegerType, StringType,
    DateType, TimestampType, DecimalType, ArrayType,
)

dbutils.widgets.text("source_table", "retaildp.bronze.olist_orders", "Source Bronze Table (orders)")
SOURCE_TABLE = dbutils.widgets.get("source_table")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Shared helpers

# COMMAND ----------

# MAGIC %run ../_shared/surrogate_keys

# COMMAND ----------

# MAGIC %run ../_shared/quarantine

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

TARGET_TABLE        = "retaildp.silver.sa_tran_head"
QUARANTINE_TABLE    = "retaildp.quarantine.silver_sa_tran_head_olist_rejects"
ITEMS_TABLE         = "retaildp.bronze.olist_order_items"
STORE_DAY_TABLE     = "retaildp.silver.sa_store_day"
STORE_MASTER_TABLE  = "retaildp.silver.sa_store_data"
FX_TABLE            = "retaildp.bronze.fx_rates"

# Olist virtual store — single store under which all Brazilian e-commerce orders aggregate.
OLIST_STORE         = 99999

# Olist-local valid TRAN_TYPE set. The full silver superset (POS) is wider; we validate
# OMS-channel rows against OMS's subset.
VALID_OLIST_TRAN_TYPES = {"SALE", "CREFUND"}

# Sentinel for NOT NULL columns with no Olist equivalent
OLIST_REGISTER_SENTINEL = "OLIST"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pre-flight diagnostics

# COMMAND ----------

print("=== sa_store_day spine check (Olist rows must exist) ===")
spine_olist_rows = (
    spark.table(STORE_DAY_TABLE)
    .where(col("STORE") == OLIST_STORE)
    .count()
)
if spine_olist_rows == 0:
    raise AssertionError(
        f"FATAL: no STORE={OLIST_STORE} rows in {STORE_DAY_TABLE}. "
        "Run sa_store_day_olist.py first; this notebook reads the spine for STORE_DAY_SEQ_NO."
    )
print(f"OK — {spine_olist_rows} Olist spine rows present")

print("\n=== bronze.fx_rates BRL coverage ===")
display(spark.sql(f"""
    SELECT MIN(rate_date) AS min_date, MAX(rate_date) AS max_date, COUNT(*) AS row_count
    FROM {FX_TABLE}
    WHERE from_currency = 'BRL' AND to_currency = 'USD'
"""))

print("\n=== bronze.olist_orders + olist_order_items row counts ===")
print(f"olist_orders:      {spark.table(SOURCE_TABLE).count():,}")
print(f"olist_order_items: {spark.table(ITEMS_TABLE).count():,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema — reused from existing table
# MAGIC
# MAGIC `sa_tran_head` was bootstrapped by the POS notebook. Read the column list at
# MAGIC runtime and project to it — keeps all three channel notebooks in lockstep.

# COMMAND ----------

assert spark.catalog.tableExists(TARGET_TABLE), (
    f"{TARGET_TABLE} does not exist. Run 01_sa_tran_head/sa_tran_head_pos.py first."
)

target_schema  = spark.table(TARGET_TABLE).schema
TARGET_COLUMNS = [f.name for f in target_schema.fields]
print(f"Target schema has {len(TARGET_COLUMNS)} columns.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — pre-aggregate items
# MAGIC
# MAGIC Two child-side projections per order:
# MAGIC
# MAGIC - `order_value` — `SUM(price + freight_value)` grouped by `order_id`. Becomes
# MAGIC   `VALUE` on the head. Reconciles cleanly when `sa_tran_item_olist.py` materialises
# MAGIC   one row per line + one synthetic freight line.
# MAGIC - `primary_seller` — `seller_id` filtered to `order_item_id = 1` per `order_id`.
# MAGIC   Becomes `VENDOR_NO`. Per-line `seller_id` is preserved in `sa_tran_item.REF_NO5`
# MAGIC   for multi-seller orders (locked decision; that's the next notebook's job).

# COMMAND ----------

order_value = (
    spark.table(ITEMS_TABLE)
    .groupBy("order_id")
    .agg(f_sum(col("price") + col("freight_value")).alias("_value"))
)

primary_seller = (
    spark.table(ITEMS_TABLE)
    .filter(col("order_item_id") == 1)
    .select(
        col("order_id"),
        col("seller_id").alias("_vendor_no"),
    )
)

print(f"Distinct orders with items: {order_value.count():,}")
print(f"Distinct orders with order_item_id=1 (primary seller resolvable): {primary_seller.count():,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — join orders to items + flatten + conform
# MAGIC
# MAGIC LEFT JOIN so orders with no items (canceled before any line written) flow through
# MAGIC to DQ rejection rather than silently dropping. The user-confirmed policy is
# MAGIC "quarantine — no seller is suspicious".

# COMMAND ----------

flat = (
    spark.table(SOURCE_TABLE)
    .join(order_value,    "order_id", "left")
    .join(primary_seller, "order_id", "left")
    .select(
        lit("OMS").alias("RTLOG_ORIG_SYS"),
        lit(OLIST_STORE).cast(LongType()).alias("STORE"),

        # DECISION 1: BUSINESS_DATE = COALESCE(approved_at, purchase_timestamp)::date.
        # MUST match the expression in sa_store_day_olist.py for FK alignment.
        to_date(coalesce(col("order_approved_at"), col("order_purchase_timestamp")))
            .alias("BUSINESS_DATE"),

        # DECISION 2: TRAN_DATETIME = purchase_timestamp (wall-clock).
        # Used by tran_seq_no_expr() as the surrogate tie-breaker.
        col("order_purchase_timestamp").cast(TimestampType()).alias("TRAN_DATETIME"),

        # DECISION 3: order_id IS the natural unique. tran_seq_no_expr() will hash it
        # together with RTLOG_ORIG_SYS + TRAN_DATETIME.
        col("order_id").alias("TRAN_SEQ_NO_NATURAL"),

        # DECISION 4: REGISTER sentinel — NOT NULL constraint.
        lit(OLIST_REGISTER_SENTINEL).alias("REGISTER"),

        # DECISION 5: TRAN_NO = xxhash64(order_id). Deterministic LongType for the
        # NOT NULL schema column.
        xxhash64(col("order_id")).alias("TRAN_NO"),

        # DECISION 7: status → TRAN_TYPE. Unknown statuses produce NULL → DQ rejects.
        when(col("order_status").isin(
                "delivered", "shipped", "processing", "approved", "created", "invoiced"
             ), lit("SALE"))
        .when(col("order_status").isin("canceled", "unavailable"),
             lit("CREFUND"))
        .otherwise(lit(None).cast(StringType()))
        .alias("TRAN_TYPE"),

        # DECISION 8: SUB_TRAN_TYPE = UPPER(status). Full string, not truncated.
        upper(col("order_status")).alias("SUB_TRAN_TYPE"),

        # Preserve raw status for traceability
        col("order_status").alias("STATUS"),

        # DECISION 9: VALUE rolled up from items. NULL if order has zero items → DQ rejects.
        col("_value").cast(DecimalType(20, 4)).alias("VALUE"),

        # DECISION 6: VENDOR_NO = primary seller (order_item_id=1). NULL if no items → DQ rejects.
        col("_vendor_no").alias("VENDOR_NO"),

        # DECISION 11: marketplace + customer_id in REF_NO1 / REF_NO2 (parallel to MKT)
        lit("OLIST_BR").alias("REF_NO1"),
        col("customer_id").alias("REF_NO2"),
    )
)

print(f"Flattened: {flat.count():,} candidate head rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — surrogate key + dimension joins + FX

# COMMAND ----------

# 3a. Surrogate key
keyed = flat.withColumn("TRAN_SEQ_NO", tran_seq_no_expr())

# 3b. Dimension join — sa_store_data (CURRENCY_CODE, COUNTRY)
store_data = (
    spark.table(STORE_MASTER_TABLE)
    .select(
        col("STORE").alias("_sd_STORE"),
        col("CURRENCY_CODE").alias("_sd_CURRENCY_CODE"),
        col("COUNTRY").alias("_sd_COUNTRY"),
    )
)
enriched_sd = (
    keyed
    .join(broadcast(store_data), col("STORE") == col("_sd_STORE"), "left")
    .withColumn("CURRENCY_CODE", col("_sd_CURRENCY_CODE"))
    .withColumn("COUNTRY",       col("_sd_COUNTRY"))
    .drop("_sd_STORE", "_sd_CURRENCY_CODE", "_sd_COUNTRY")
)

# 3c. Dimension join — sa_store_day (STORE_DAY_SEQ_NO)
store_day = (
    spark.table(STORE_DAY_TABLE)
    .select(
        col("STORE").alias("_sday_STORE"),
        col("BUSINESS_DATE").alias("_sday_DATE"),
        col("STORE_DAY_SEQ_NO").alias("_sday_SEQ_NO"),
    )
)
enriched_sday = (
    enriched_sd
    .join(
        broadcast(store_day),
        (col("STORE") == col("_sday_STORE")) & (col("BUSINESS_DATE") == col("_sday_DATE")),
        "left",
    )
    .withColumn("STORE_DAY_SEQ_NO", col("_sday_SEQ_NO"))
    .drop("_sday_STORE", "_sday_DATE", "_sday_SEQ_NO")
)

# 3d. FX enrich — broadcast bronze.fx_rates on (business_date, currency)
fx = (
    spark.table(FX_TABLE)
    .filter(col("to_currency") == "USD")
    .select(
        col("rate_date").alias("_fx_DATE"),
        col("from_currency").alias("_fx_CURR"),
        col("rate").cast(DecimalType(20, 6)).alias("_fx_RATE"),
    )
)
enriched_fx = (
    enriched_sday
    .join(
        broadcast(fx),
        (col("BUSINESS_DATE") == col("_fx_DATE")) & (col("CURRENCY_CODE") == col("_fx_CURR")),
        "left",
    )
    .withColumn("FX_RATE", col("_fx_RATE"))
    .drop("_fx_DATE", "_fx_CURR", "_fx_RATE")
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — derive TAX_MODE + VALUE_USD + lineage + explicit nulls

# COMMAND ----------

derived = (
    enriched_fx
    # DECISION 10: TAX_MODE extension — Brazil (ICMS) joins India in the IGTAX bucket.
    .withColumn(
        "TAX_MODE",
        when(col("COUNTRY").isin("IND", "BRA"),       lit("IGTAX"))
        .when(col("COUNTRY").isin("USA", "GBR"),      lit("TAX"))
        .when(col("COUNTRY").isin("ARE", "SGP"),      lit("BOTH"))
        .otherwise(lit(None).cast(StringType())),
    )
    .withColumn("VALUE_USD", (col("VALUE") * col("FX_RATE")).cast(DecimalType(20, 4)))

    # Olist doesn't populate these — explicit NULLs keep schema projection happy
    .withColumn("BANNER_NO",       lit(None).cast(IntegerType()))
    .withColumn("CASHIER",         lit(None).cast(StringType()))
    .withColumn("SALESPERSON",     lit(None).cast(StringType()))
    .withColumn("POS_TRAN_IND",    lit(None).cast(StringType()))
    .withColumn("ERROR_IND",       lit(None).cast(StringType()))
    .withColumn("ORIG_TRAN_NO",    lit(None).cast(StringType()))
    .withColumn("ORIG_TRAN_TYPE",  lit(None).cast(StringType()))
    .withColumn("ORIG_REG_NO",     lit(None).cast(StringType()))
    .withColumn("REV_NO",          lit(None).cast(IntegerType()))
    .withColumn("REASON_CODE",     lit(None).cast(StringType()))
    .withColumn("VENDOR_INVC_NO",  lit(None).cast(StringType()))
    .withColumn("UPDATE_DATETIME", lit(None).cast(TimestampType()))
    .withColumn("UPDATE_ID",       lit(None).cast(StringType()))

    .withColumn("_silver_ts", current_timestamp())
    .withColumn("_source",    lit(SOURCE_TABLE))
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — DQ split

# COMMAND ----------

dq = derived.withColumn(
    "rejection_reason",
    array_compact(array(
        when(col("TRAN_DATETIME").isNull(),
             lit("TRAN_DATETIME null — order_purchase_timestamp missing")),
        when(col("BUSINESS_DATE").isNull(),
             lit("BUSINESS_DATE null — both order_approved_at and order_purchase_timestamp missing")),
        when(~col("TRAN_TYPE").isin(*VALID_OLIST_TRAN_TYPES),
             lit("TRAN_TYPE not in OMS-valid set {SALE, CREFUND}; status mapping failed")),
        when(col("VALUE").isNull(),
             lit("VALUE null — order has no items (cannot roll up price+freight)")),
        when(col("VENDOR_NO").isNull(),
             lit("VENDOR_NO null — order has no primary seller (no order_item_id=1 row)")),
        when(col("STORE_DAY_SEQ_NO").isNull(),
             lit("STORE_DAY_SEQ_NO FK lookup failed (no matching sa_store_day row)")),
        when(col("CURRENCY_CODE").isNull(),
             lit("CURRENCY_CODE FK lookup failed (STORE=99999 missing in sa_store_data — pre-flight)")),
        when(col("TAX_MODE").isNull(),
             lit("TAX_MODE could not be derived (unknown COUNTRY)")),
    )),
)

clean   = dq.filter("size(rejection_reason) = 0").drop("rejection_reason")
rejects = dq.filter("size(rejection_reason) > 0")
# _quarantine_ts added inside merge_and_quarantine

# Project clean to exact target column order (sourced from existing table)
clean = clean.select(*TARGET_COLUMNS)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6 — MERGE clean + append rejects

# COMMAND ----------

clean_n, reject_n = merge_and_quarantine(
    clean_df=clean,
    rejects_df=rejects,
    target_table=TARGET_TABLE,
    quarantine_table=QUARANTINE_TABLE,
    merge_keys=["TRAN_SEQ_NO"],
)
print(f"Olist Pass-3: clean={clean_n:,} rejects={reject_n:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Validation (channel-filtered to OMS)

# COMMAND ----------

oms = spark.table(TARGET_TABLE).where(col("RTLOG_ORIG_SYS") == "OMS")
oms_count = oms.count()
total_count = spark.table(TARGET_TABLE).count()
print(f"silver.sa_tran_head OMS row count:    {oms_count:,}")
print(f"silver.sa_tran_head total row count:  {total_count:,}")

if spark.catalog.tableExists(QUARANTINE_TABLE):
    q_count = spark.table(QUARANTINE_TABLE).count()
    print(f"Olist quarantine row count:           {q_count:,}")
    if q_count > 0:
        print("\nTop Olist rejection reasons:")
        (
            spark.table(QUARANTINE_TABLE)
            .selectExpr("explode(rejection_reason) as reason")
            .groupBy("reason").count()
            .orderBy(col("count").desc())
            .show(truncate=False)
        )
else:
    print("Olist quarantine row count:           0 (table not created)")

if oms_count > 0:
    # 1. PK uniqueness within OMS
    dup_count = oms.groupBy("TRAN_SEQ_NO").count().where("count > 1").count()
    assert dup_count == 0, f"PK violation: {dup_count} duplicate TRAN_SEQ_NO within OMS"
    print("PK uniqueness check passed (within OMS)")

    # 2. CROSS-CHANNEL COLLISION CHECK — channel-aware surrogate must keep POS/MKT/OMS disjoint
    pos_mkt = spark.table(TARGET_TABLE).where(col("RTLOG_ORIG_SYS") != "OMS")
    overlap = oms.join(pos_mkt, "TRAN_SEQ_NO", "inner").count()
    assert overlap == 0, f"Cross-channel collision: {overlap} TRAN_SEQ_NO shared with POS/MKT"
    print("Cross-channel disjoint check passed (OMS vs POS/MKT)")

    # 3. TRAN_TYPE domain — OMS subset
    bad_tt = oms.where(~col("TRAN_TYPE").isin(*VALID_OLIST_TRAN_TYPES)).count()
    assert bad_tt == 0, f"{bad_tt} OMS rows with invalid TRAN_TYPE"
    print("OMS TRAN_TYPE domain check passed")

    # 4. CURRENCY_CODE must be BRL for all OMS rows
    non_brl = oms.where(col("CURRENCY_CODE") != "BRL").count()
    assert non_brl == 0, f"{non_brl} OMS rows with CURRENCY_CODE != BRL"
    print("CURRENCY_CODE=BRL check passed")

    # 5. TAX_MODE must be IGTAX for all OMS rows
    bad_tm = oms.where(col("TAX_MODE") != "IGTAX").count()
    assert bad_tm == 0, f"{bad_tm} OMS rows with TAX_MODE != IGTAX"
    print("TAX_MODE=IGTAX check passed")

    # 6. FX coverage diagnostic
    null_fx = oms.where(col("FX_RATE").isNull()).count()
    print(f"\nFX_RATE null count (BRL gap days): {null_fx:,}  ({100.0*null_fx/oms_count:.2f}% of OMS)")

    # 7. TRAN_TYPE distribution
    print("\nOMS TRAN_TYPE distribution:")
    display(oms.groupBy("TRAN_TYPE").count().orderBy(col("count").desc()))

    # 8. SUB_TRAN_TYPE distribution (Olist status detail)
    print("\nOMS SUB_TRAN_TYPE distribution:")
    display(oms.groupBy("SUB_TRAN_TYPE").count().orderBy(col("count").desc()))

    # 9. VALUE summary (BRL)
    print("\nOMS VALUE summary (BRL):")
    display(oms.selectExpr(
        "ROUND(MIN(VALUE), 2) AS min_value",
        "ROUND(MAX(VALUE), 2) AS max_value",
        "ROUND(AVG(VALUE), 2) AS avg_value",
        "ROUND(SUM(VALUE), 2) AS total_brl",
    ))

    # 10. Sample
    print("\nSample rows:")
    display(oms.orderBy("BUSINESS_DATE").limit(5))
