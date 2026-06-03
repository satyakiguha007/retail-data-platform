# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_tran_item` (Olist additive)
# MAGIC
# MAGIC Third channel writer for the item-line table. Each Olist order produces:
# MAGIC - **N real lines** — one row per `olist_order_items.order_item_id` (the merchandise lines)
# MAGIC - **1 synthetic freight line** — collapses all per-line `freight_value` into one NMR
# MAGIC   ("non-merchandise") row so the reconciliation invariant `head.VALUE = SUM(item.QTY × item.UNIT_RETAIL)`
# MAGIC   holds for every order.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source (items)** | `retaildp.bronze.olist_order_items` |
# MAGIC | **Source (orders)** | `retaildp.bronze.olist_orders` (for `order_purchase_timestamp` — surrogate input) |
# MAGIC | **Target** | `retaildp.silver.sa_tran_item` (additive — POS bootstraps it) |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_tran_item_olist_rejects` |
# MAGIC | **Parent FK** | `silver.sa_tran_head` on `TRAN_SEQ_NO` (inherits CURRENCY_CODE + FX_RATE) |
# MAGIC | **Pattern** | Batch UNION ALL (real + synthetic) → MERGE |
# MAGIC | **Idempotent** | Yes — deterministic `(TRAN_SEQ_NO, ITEM_SEQ_NO)` + MERGE |
# MAGIC | **Streaming** | No — Olist is static |
# MAGIC
# MAGIC ## What's new vs POS/MKT siblings
# MAGIC
# MAGIC 1. **Flat source (no `explode`).** POS reads from `bronze.pos_rtlog` which has `tran_item`
# MAGIC    as a nested array — POS explodes one row → N item rows. MKT does the same. Olist's
# MAGIC    `bronze.olist_order_items` is already one row per item line — no explode needed.
# MAGIC
# MAGIC 2. **Synthetic line materialisation.** Olist bronze has no freight row — freight value
# MAGIC    lives as a per-line attribute on each item. To make the reconciliation invariant
# MAGIC    hold (head.VALUE rolled up in `sa_tran_head_olist.py` = `SUM(price + freight_value)`),
# MAGIC    we materialise one NMR line per order with `UNIT_RETAIL = SUM(freight_value)`.
# MAGIC    Sentinel SKU = `'101010101'`, `ITEM_SEQ_NO = MAX(order_item_id) + 1` so it sorts last.
# MAGIC
# MAGIC 3. **Parent timestamp join.** The surrogate `tran_seq_no_expr()` needs `TRAN_DATETIME`,
# MAGIC    but `olist_order_items` only has `shipping_limit_date`. Must join `olist_orders` to
# MAGIC    pull `order_purchase_timestamp` — exact same value POS/MKT used as the hash tie-breaker.
# MAGIC
# MAGIC ## Locked decisions
# MAGIC
# MAGIC | # | Decision | Rationale |
# MAGIC |---|---|---|
# MAGIC | 1 | `ITEM = product_id` for real lines, `'101010101'` for freight | Product hash = ReSA-style SKU; freight sentinel is the brief's locked value |
# MAGIC | 2 | `ITEM_TYPE = 'ITM'` for real lines, `'NMR'` for freight | Oracle Retail convention: ITM = merchandise, NMR = non-merchandise (shipping, fees) |
# MAGIC | 3 | `ITEM_SEQ_NO = order_item_id` for real lines, `MAX(order_item_id)+1` for freight | Brief-locked; freight sorts last visually |
# MAGIC | 4 | `QTY = 1` everywhere (real + freight) | Olist's `order_item_id` enumerates physical units — qty 2 = two rows, not qty=2 on one row |
# MAGIC | 5 | `UNIT_RETAIL = price` (real), `SUM(freight_value)` per order (freight) | Reconciles head.VALUE cleanly |
# MAGIC | 6 | `REF_NO5 = seller_id` for real lines, NULL for freight | Per-line seller granularity (multi-seller orders); freight isn't seller-attributable |
# MAGIC | 7 | DEPT/CLASS/SUBCLASS NULL everywhere (real + freight) | Olist bronze has no merch hierarchy; per brief |
# MAGIC | 8 | `TOTAL_IGTAX_AMT` NULL everywhere | Olist has no tax breakout; "not measured" not "measured zero" |
# MAGIC | 9 | Parent `TRAN_DATETIME` = `order_purchase_timestamp` (join from olist_orders) | Must match `sa_tran_head_olist.py` exactly for FK alignment |
# MAGIC
# MAGIC ## DQ rules (failures routed to quarantine)
# MAGIC 1. Parent FK — `(TRAN_SEQ_NO,)` must resolve in `sa_tran_head` (orphan check via `_has_parent`)
# MAGIC 2. `ITEM_SEQ_NO` NOT NULL (PK)
# MAGIC 3. `ITEM_TYPE` NOT NULL (NOT NULL in schema)
# MAGIC 4. `ITEM` NOT NULL (real lines need product_id, freight uses sentinel)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, current_timestamp, lit, when, broadcast,
    array, array_compact,
    sum as f_sum, max as f_max,
)
from pyspark.sql.types import (
    StructType, StructField, LongType, IntegerType, StringType,
    DateType, TimestampType, DecimalType, ArrayType,
)

dbutils.widgets.text("items_source",  "retaildp.bronze.olist_order_items", "Source Bronze Table (items)")
dbutils.widgets.text("orders_source", "retaildp.bronze.olist_orders",      "Source Bronze Table (orders, for parent ts)")
ITEMS_SOURCE  = dbutils.widgets.get("items_source")
ORDERS_SOURCE = dbutils.widgets.get("orders_source")

# Composite source identifier for the lineage stamp
SOURCE_TABLE = f"{ITEMS_SOURCE}+{ORDERS_SOURCE}"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Shared helpers

# COMMAND ----------

# MAGIC %run ../_shared/surrogate_keys

# COMMAND ----------

# MAGIC %run ../_shared/fx_helpers

# COMMAND ----------

# MAGIC %run ../_shared/quarantine

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

TARGET_TABLE     = "retaildp.silver.sa_tran_item"
QUARANTINE_TABLE = "retaildp.quarantine.silver_sa_tran_item_olist_rejects"
PARENT_TABLE     = "retaildp.silver.sa_tran_head"

OLIST_STORE      = 99999
FREIGHT_SKU      = "101010101"
ITEM_TYPE_REAL   = "ITM"
ITEM_TYPE_NMR    = "NMR"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pre-flight diagnostics

# COMMAND ----------

print("=== sa_tran_head parent rows for OMS ===")
oms_head_count = (
    spark.table(PARENT_TABLE).where(col("RTLOG_ORIG_SYS") == "OMS").count()
)
if oms_head_count == 0:
    raise AssertionError(
        f"FATAL: no OMS rows in {PARENT_TABLE}. "
        "Run sa_tran_head_olist.py first; this notebook FK-joins to it."
    )
print(f"OK — {oms_head_count:,} OMS parent rows in {PARENT_TABLE}")

print("\n=== Source schemas ===")
print(f"{ITEMS_SOURCE}  rows: {spark.table(ITEMS_SOURCE).count():,}")
print(f"{ORDERS_SOURCE} rows: {spark.table(ORDERS_SOURCE).count():,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema — reused from existing table

# COMMAND ----------

assert spark.catalog.tableExists(TARGET_TABLE), (
    f"{TARGET_TABLE} does not exist. Run 02_sa_tran_item/sa_tran_item_pos.py first."
)

target_schema  = spark.table(TARGET_TABLE).schema
TARGET_COLUMNS = [f.name for f in target_schema.fields]
print(f"Target schema has {len(TARGET_COLUMNS)} columns.")

# Sanity check — schema migration prerequisite
required_extensions = {"REF_NO5", "REF_NO6", "REF_NO7", "REF_NO8"}
missing = required_extensions - set(TARGET_COLUMNS)
assert not missing, (
    f"Target {TARGET_TABLE} is missing required columns: {missing}. "
    "The REF_NO5-8 schema migration must run before this notebook."
)
print("REF_NO5-8 schema extension present — OK")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — join items + orders, derive per-order aggregates
# MAGIC
# MAGIC The items table doesn't carry `order_purchase_timestamp` — we need it for the
# MAGIC parent surrogate hash. Inner join on `order_id`: items orphaned from orders
# MAGIC can't compute a valid `TRAN_SEQ_NO` anyway, so dropping them here is correct.
# MAGIC
# MAGIC Same step computes the per-order aggregates the synthetic freight line needs:
# MAGIC `MAX(order_item_id)` for the ITEM_SEQ_NO bump and `SUM(freight_value)` for UNIT_RETAIL.

# COMMAND ----------

orders_keyed = (
    spark.table(ORDERS_SOURCE)
    .select(
        col("order_id"),
        col("order_purchase_timestamp").cast(TimestampType()).alias("_parent_ts"),
    )
)

items_with_parent = (
    spark.table(ITEMS_SOURCE)
    .join(orders_keyed, "order_id", "inner")
)

per_order = (
    items_with_parent
    .groupBy("order_id", "_parent_ts")
    .agg(
        f_max("order_item_id").alias("_max_item_id"),
        f_sum("freight_value").alias("_freight_total"),
    )
)

print(f"Items joined to parent orders: {items_with_parent.count():,}")
print(f"Distinct orders producing a freight line: {per_order.count():,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — build the real-item rows

# COMMAND ----------

real_items = (
    items_with_parent
    .select(
        # Parent surrogate inputs — same expression as sa_tran_head_olist.py
        lit("OMS").alias("RTLOG_ORIG_SYS"),
        lit(OLIST_STORE).cast(LongType()).alias("STORE"),
        col("order_id").alias("TRAN_SEQ_NO_NATURAL"),
        col("_parent_ts").alias("TRAN_DATETIME"),

        # Line identity
        col("order_item_id").cast(IntegerType()).alias("ITEM_SEQ_NO"),

        # Item attributes
        col("product_id").alias("ITEM"),
        lit(ITEM_TYPE_REAL).alias("ITEM_TYPE"),

        # Quantities — Olist enumerates physical units as separate rows; QTY=1 per line
        lit(1).cast(DecimalType(12, 4)).alias("QTY"),
        col("price").cast(DecimalType(20, 4)).alias("UNIT_RETAIL"),

        # REF_NO5 carries the per-line seller (multi-seller order granularity)
        col("seller_id").alias("REF_NO5"),
    )
)

print(f"Real item rows: {real_items.count():,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — build the synthetic freight rows (one per order)

# COMMAND ----------

freight_items = (
    per_order
    .select(
        # Parent surrogate inputs — IDENTICAL to real-items + sa_tran_head_olist.py
        lit("OMS").alias("RTLOG_ORIG_SYS"),
        lit(OLIST_STORE).cast(LongType()).alias("STORE"),
        col("order_id").alias("TRAN_SEQ_NO_NATURAL"),
        col("_parent_ts").alias("TRAN_DATETIME"),

        # Line identity — bumped past the last real item
        (col("_max_item_id") + lit(1)).cast(IntegerType()).alias("ITEM_SEQ_NO"),

        # Item attributes — sentinel SKU + non-merch type
        lit(FREIGHT_SKU).alias("ITEM"),
        lit(ITEM_TYPE_NMR).alias("ITEM_TYPE"),

        # Quantities — QTY=1, UNIT_RETAIL = roll-up of all per-line freight on the order
        lit(1).cast(DecimalType(12, 4)).alias("QTY"),
        col("_freight_total").cast(DecimalType(20, 4)).alias("UNIT_RETAIL"),

        # REF_NO5 NULL — freight isn't seller-attributable
        lit(None).cast(StringType()).alias("REF_NO5"),
    )
)

print(f"Synthetic freight rows: {freight_items.count():,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — UNION + parent FK enrich + USD + lineage + explicit nulls

# COMMAND ----------

combined = real_items.unionByName(freight_items)
print(f"Combined item rows (real + freight): {combined.count():,}")

# 4a. Surrogate key — same hash as sa_tran_head_olist.py
keyed = combined.withColumn("TRAN_SEQ_NO", tran_seq_no_expr())

# 4b. FK enrich — inherits CURRENCY_CODE + FX_RATE from sa_tran_head, flags orphans
enriched = enrich_with_parent_fx(keyed, PARENT_TABLE, ["TRAN_SEQ_NO"])

# 4c. BUSINESS_DATE — need this for the partition column. The parent has it; pull along.
#     enrich_with_parent_fx only inherits CURRENCY_CODE + FX_RATE — fetch BUSINESS_DATE separately.
parent_bd = (
    spark.table(PARENT_TABLE)
    .select(
        col("TRAN_SEQ_NO").alias("_pbd_TRAN_SEQ_NO"),
        col("BUSINESS_DATE").alias("_pbd_BUSINESS_DATE"),
    )
)
enriched = (
    enriched
    .join(broadcast(parent_bd), col("TRAN_SEQ_NO") == col("_pbd_TRAN_SEQ_NO"), "left")
    .withColumn("BUSINESS_DATE", col("_pbd_BUSINESS_DATE"))
    .drop("_pbd_TRAN_SEQ_NO", "_pbd_BUSINESS_DATE")
)

# 4d. Derive USD companions + lineage + explicit nulls for fields Olist doesn't populate
derived = (
    enriched
    .withColumn("UNIT_RETAIL_USD",
                (col("UNIT_RETAIL") * col("FX_RATE")).cast(DecimalType(20, 4)))

    # Olist doesn't populate these — explicit NULLs keep schema projection happy
    .withColumn("ITEM_STATUS",          lit(None).cast(StringType()))
    .withColumn("ITEM_SWIPED_IND",      lit(None).cast(StringType()))
    .withColumn("DROP_SHIP_IND",        lit(None).cast(StringType()))
    .withColumn("DEPT",                 lit(None).cast(IntegerType()))
    .withColumn("CLASS",                lit(None).cast(IntegerType()))
    .withColumn("SUBCLASS",             lit(None).cast(IntegerType()))
    .withColumn("SELLING_UOM",          lit(None).cast(StringType()))
    .withColumn("UOM_QUANTITY",         lit(None).cast(DecimalType(12, 4)))
    .withColumn("ORIG_UNIT_RETAIL",     lit(None).cast(DecimalType(20, 4)))
    .withColumn("OVERRIDE_REASON",      lit(None).cast(StringType()))
    .withColumn("TAX_IND",              lit(None).cast(StringType()))
    .withColumn("UNIT_RETAIL_VAT_INCL", lit(None).cast(StringType()))
    .withColumn("TOTAL_IGTAX_AMT",      lit(None).cast(DecimalType(20, 4)))
    .withColumn("RETURN_REASON_CODE",   lit(None).cast(StringType()))
    .withColumn("CUST_ORDER_NO",        lit(None).cast(StringType()))
    .withColumn("ERROR_IND",            lit(None).cast(StringType()))
    .withColumn("ORIG_UNIT_RETAIL_USD", lit(None).cast(DecimalType(20, 4)))
    .withColumn("TOTAL_IGTAX_AMT_USD",  lit(None).cast(DecimalType(20, 4)))
    .withColumn("REF_NO6",              lit(None).cast(StringType()))
    .withColumn("REF_NO7",              lit(None).cast(StringType()))
    .withColumn("REF_NO8",              lit(None).cast(StringType()))

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
        when(~col("_has_parent"),
             lit("orphan_no_parent_header (TRAN_SEQ_NO not in sa_tran_head — order quarantined upstream)")),
        when(col("ITEM_SEQ_NO").isNull(),
             lit("ITEM_SEQ_NO null — cannot form PK")),
        when(col("ITEM_TYPE").isNull(),
             lit("ITEM_TYPE null — mandatory in ReSA")),
        when(col("ITEM").isNull(),
             lit("ITEM null — product_id missing on real line OR freight SKU literal failed (shouldn't happen)")),
    )),
)

clean   = dq.filter("size(rejection_reason) = 0").drop("rejection_reason", "_has_parent")
rejects = dq.filter("size(rejection_reason) > 0").drop("_has_parent")
# _quarantine_ts added by merge_and_quarantine

# Project clean to exact target schema column order
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
    merge_keys=["TRAN_SEQ_NO", "ITEM_SEQ_NO"],
)
print(f"Olist Pass-3 items: clean={clean_n:,} rejects={reject_n:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Validation (channel-filtered to OMS)

# COMMAND ----------

oms = spark.table(TARGET_TABLE).where(col("RTLOG_ORIG_SYS") == "OMS")
oms_count = oms.count()
total_count = spark.table(TARGET_TABLE).count()
print(f"silver.sa_tran_item OMS row count:    {oms_count:,}")
print(f"silver.sa_tran_item total row count:  {total_count:,}")

if spark.catalog.tableExists(QUARANTINE_TABLE):
    q_count = spark.table(QUARANTINE_TABLE).count()
    print(f"Olist items quarantine row count:     {q_count:,}")
    if q_count > 0:
        print("\nTop Olist items rejection reasons:")
        (
            spark.table(QUARANTINE_TABLE)
            .selectExpr("explode(rejection_reason) as reason")
            .groupBy("reason").count()
            .orderBy(col("count").desc())
            .show(truncate=False)
        )

if oms_count > 0:
    # 1. PK uniqueness within OMS
    dup_count = oms.groupBy("TRAN_SEQ_NO", "ITEM_SEQ_NO").count().where("count > 1").count()
    assert dup_count == 0, f"PK violation: {dup_count} duplicate (TRAN_SEQ_NO, ITEM_SEQ_NO) within OMS"
    print("PK uniqueness check passed (within OMS)")

    # 2. Cross-channel disjoint check on TRAN_SEQ_NO
    pos_mkt_seqs = spark.table(TARGET_TABLE).where(col("RTLOG_ORIG_SYS") != "OMS").select("TRAN_SEQ_NO").distinct()
    oms_seqs     = oms.select("TRAN_SEQ_NO").distinct()
    overlap = oms_seqs.join(pos_mkt_seqs, "TRAN_SEQ_NO", "inner").count()
    assert overlap == 0, f"Cross-channel collision: {overlap} TRAN_SEQ_NO shared with POS/MKT"
    print("Cross-channel disjoint check passed (OMS vs POS/MKT)")

    # 3. ITEM_TYPE distribution — should be ITM dominant, NMR ≈ #orders
    print("\nOMS ITEM_TYPE distribution:")
    display(oms.groupBy("ITEM_TYPE").count().orderBy(col("count").desc()))

    # 4. THE BIG ONE — head.VALUE == SUM(item.QTY * item.UNIT_RETAIL) per order
    #    This is the reason the freight line exists. Zero tolerance: any drift fails the assert.
    head_oms = spark.table(PARENT_TABLE).where(col("RTLOG_ORIG_SYS") == "OMS").select("TRAN_SEQ_NO", "VALUE")
    item_totals = (
        oms
        .selectExpr("TRAN_SEQ_NO", "CAST(QTY * UNIT_RETAIL AS DECIMAL(20,4)) AS _line_total")
        .groupBy("TRAN_SEQ_NO")
        .agg(f_sum("_line_total").cast(DecimalType(20, 4)).alias("item_roll_up"))
    )
    recon = (
        head_oms.alias("h")
        .join(item_totals.alias("i"), "TRAN_SEQ_NO", "inner")
        .selectExpr(
            "TRAN_SEQ_NO",
            "CAST(h.VALUE AS DECIMAL(20,4)) AS head_value",
            "i.item_roll_up",
            "ABS(h.VALUE - i.item_roll_up) AS delta",
        )
    )
    drift = recon.where(col("delta") > 0.01).count()
    print(f"\nReconciliation (head.VALUE vs SUM(item.QTY*UNIT_RETAIL)):")
    print(f"  Orders reconciled: {recon.count():,}")
    print(f"  Drift > 0.01 BRL : {drift}")
    if drift > 0:
        print("  Sample drifts (first 10):")
        recon.where(col("delta") > 0.01).orderBy(col("delta").desc()).show(10, truncate=False)
    assert drift == 0, f"Reconciliation failed: {drift} orders with head.VALUE != SUM(item lines)"
    print("Reconciliation invariant passed (head.VALUE == SUM(item.QTY * UNIT_RETAIL))")

    # 5. Sample — pick one order, show its full line set (real + freight)
    print("\nSample order — full line set (real items + freight, ordered by ITEM_SEQ_NO):")
    sample_tran = oms.select("TRAN_SEQ_NO").limit(1).collect()[0].TRAN_SEQ_NO
    display(
        oms.where(col("TRAN_SEQ_NO") == sample_tran)
        .select("TRAN_SEQ_NO", "ITEM_SEQ_NO", "ITEM", "ITEM_TYPE", "QTY", "UNIT_RETAIL", "REF_NO5")
        .orderBy("ITEM_SEQ_NO")
    )

