# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_tran_item` (Marketplace)
# MAGIC
# MAGIC Line-item detail for marketplace orders. One row per `(order × items[] element)`,
# MAGIC written to the same `silver.sa_tran_item` table as POS, distinguished by
# MAGIC `RTLOG_ORIG_SYS='MKT'`. Child of `sa_tran_head` MKT rows; inherits
# MAGIC `CURRENCY_CODE` + `FX_RATE` from the parent.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source** | `retaildp.bronze.marketplace` — `items` is `ARRAY<STRUCT>` |
# MAGIC | **Target** | `retaildp.silver.sa_tran_item` (shared with POS) |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_tran_item_marketplace_rejects` (per-source) |
# MAGIC | **FK lookup** | `silver.sa_tran_head` (parent — validates FK AND carries `CURRENCY_CODE` / `FX_RATE`) |
# MAGIC | **Pattern** | `readStream` + `availableNow` + `foreachBatch` → MERGE |
# MAGIC | **Idempotent** | Yes — deterministic `TRAN_SEQ_NO` + MERGE on `(TRAN_SEQ_NO, ITEM_SEQ_NO)` |
# MAGIC | **Partitioned by** | `BUSINESS_DATE` (inherited from target) |
# MAGIC
# MAGIC ## Shared helpers used (see `_shared/`)
# MAGIC - `surrogate_keys.tran_seq_no_expr()` — same canonical hash as MKT head
# MAGIC - `fx_helpers.enrich_with_parent_fx()` — FK validate + inherit `CURRENCY_CODE` / `FX_RATE`
# MAGIC - `quarantine.merge_and_quarantine()` — standard idempotent MERGE (unlike `sa_store_day`,
# MAGIC   `sa_tran_item` rows are channel-owned — `whenMatchedUpdateAll` is correct here)
# MAGIC
# MAGIC ## Locked decisions (Pass-2 design choices)
# MAGIC 1. **`abs(qty)` on RETURN / CREFUND items** — bronze emits negative quantities for items
# MAGIC    on `RETURNED` / `CANCELLED_REFUND` orders. We flip to positive in silver. Direction
# MAGIC    lives on the header's `TRAN_TYPE` (`RETURN` / `CREFUND`), matching POS convention.
# MAGIC 2. **`ITEM_TYPE = 'ITM'`** — marketplace items are always merchandise. ReSA `ITEM_TYPE` is
# MAGIC    NOT NULL; constant `'ITM'` matches POS's most common item type.
# MAGIC 3. **`DEPT` / `CLASS` / `SUBCLASS` parsed from prefixed strings** — bronze emits
# MAGIC    `"D10" / "C101" / "S1001"`; silver expects `INTEGER`. `regexp_extract(...,"\d+",0)::int`
# MAGIC    strips the prefix. Parse failures → NULL (kept as data, not quarantined — same as POS).
# MAGIC 4. **`UNIT_RETAIL = item.unit_price`** — different bronze field name, same semantic.
# MAGIC 5. **Parent-key flattening MUST match `sa_tran_head_marketplace`** — `order_id` →
# MAGIC    `TRAN_SEQ_NO_NATURAL`, `settle_date::timestamp` → `TRAN_DATETIME`,
# MAGIC    `rtlog_orig_sys` → `RTLOG_ORIG_SYS`. Drift here = surrogate mismatch = every MKT
# MAGIC    item rejected as orphan.
# MAGIC
# MAGIC ## Patterns introduced here (vs `sa_tran_item_pos.py`)
# MAGIC 1. **Different bronze shape, same flattening pattern** — POS has `tran_head` struct +
# MAGIC    `tran_item` array; MKT has flat top-level fields + `items` array. Both fan out 1:N
# MAGIC    via `explode`, both produce the same target rows.
# MAGIC 2. **Per-source quarantine + checkpoint** — `…silver_sa_tran_item_marketplace_rejects`
# MAGIC    and `checkpoints/silver/sa_tran_item/marketplace/`. Same Pass-2 convention.
# MAGIC 3. **No bootstrap** — table exists from POS Pass-1. We project to `TARGET_COLUMNS`
# MAGIC    read from the existing schema.
# MAGIC 4. **`ITEM_TYPE` constant** — first time we hard-code a ReSA NOT NULL column with `lit()`.
# MAGIC    Documented as the marketplace canonicalisation, not a kludge.
# MAGIC 5. **POS-only fields explicit-NULL'd** — 13 columns have no MKT equivalent
# MAGIC    (`ITEM_STATUS`, `ITEM_SWIPED_IND`, `DROP_SHIP_IND`, `SELLING_UOM`, `UOM_QUANTITY`,
# MAGIC    `ORIG_UNIT_RETAIL`, `OVERRIDE_REASON`, `TAX_IND`, `UNIT_RETAIL_VAT_INCL`,
# MAGIC    `TOTAL_IGTAX_AMT`, `RETURN_REASON_CODE`, `CUST_ORDER_NO`, `ERROR_IND`).
# MAGIC    Same evolution discipline as the MKT head notebook.
# MAGIC
# MAGIC ## DQ rules (failures routed to quarantine)
# MAGIC 1. `orphan_no_parent_header` — `TRAN_SEQ_NO` has no match in `silver.sa_tran_head`
# MAGIC    (should be ~0 now that the spine fix is in)
# MAGIC 2. `ITEM_SEQ_NO` is NOT NULL (PK)
# MAGIC 3. `ITEM_TYPE` is NOT NULL (we set it to `'ITM'`, so defensive only)
# MAGIC
# MAGIC ## NOT a DQ failure (kept as data, not quarantined)
# MAGIC - NULL `ITEM` (sku missing in bronze) — rare simulator-fault rows
# MAGIC - NULL `DEPT` / `CLASS` / `SUBCLASS` from parse failure — rare
# MAGIC - NULL `UNIT_RETAIL` — non-fatal; USD companion goes NULL too
# MAGIC - `FX_RATE` null → `*_USD` columns null → row still lands

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, explode, current_timestamp, lit, when,
    array, array_compact,
    regexp_extract, abs,
)
from pyspark.sql.types import (
    StructType, StructField, LongType, IntegerType, StringType,
    DateType, TimestampType, DecimalType, ArrayType,
)

dbutils.widgets.text("source_table", "retaildp.bronze.marketplace", "Source Bronze Table")
SOURCE_TABLE = dbutils.widgets.get("source_table")

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
QUARANTINE_TABLE = "retaildp.quarantine.silver_sa_tran_item_marketplace_rejects"
PARENT_TABLE     = "retaildp.silver.sa_tran_head"   # FK + CURRENCY_CODE / FX_RATE carrier
CHECKPOINT_PATH  = (
    "abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/silver/sa_tran_item/marketplace/"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema — reused from existing table
# MAGIC
# MAGIC Additive writer. POS Pass-1 bootstrapped this table; we read the column list at
# MAGIC runtime and project to it.

# COMMAND ----------

assert spark.catalog.tableExists(TARGET_TABLE), (
    f"{TARGET_TABLE} does not exist. Run 02_sa_tran_item/sa_tran_item_pos.py first "
    "to bootstrap the table; this notebook is an additive writer for the MKT channel."
)

target_schema  = spark.table(TARGET_TABLE).schema
TARGET_COLUMNS = [f.name for f in target_schema.fields]
print(f"Target schema has {len(TARGET_COLUMNS)} columns.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## foreachBatch handler
# MAGIC
# MAGIC Per micro-batch:
# MAGIC 1. **Explode + flatten** — `explode(items)` fans one bronze order into N item rows.
# MAGIC    Pull top-level fields (parent key) + `item.*` (attributes) with explicit aliases.
# MAGIC    `TRAN_DATETIME` cast to `TimestampType()` here so `tran_seq_no_expr()` produces
# MAGIC    the right hash. Parent-key fields MUST match `sa_tran_head_marketplace` exactly.
# MAGIC 2. **Surrogate** — `tran_seq_no_expr()` from `_shared/surrogate_keys`.
# MAGIC 3. **FK enrich** — `enrich_with_parent_fx()` from `_shared/fx_helpers`. Broadcast-joins
# MAGIC    `sa_tran_head` on `TRAN_SEQ_NO`, inherits `CURRENCY_CODE` + `FX_RATE`, flags
# MAGIC    orphans via `_has_parent`.
# MAGIC 4. **Derive + NULL POS-only columns** — `*_USD = amount * FX_RATE`, lineage,
# MAGIC    explicit NULLs for the 13 ReSA columns marketplace doesn't populate.
# MAGIC 5. **DQ split** — clean vs reject via `rejection_reason` array.
# MAGIC 6. **Write** — `merge_and_quarantine()`. Idempotent MERGE on
# MAGIC    `(TRAN_SEQ_NO, ITEM_SEQ_NO)` + append to quarantine.

# COMMAND ----------

def merge_microbatch(microBatchDF: DataFrame, batch_id: int) -> None:
    # 1. Explode items, then flatten with explicit aliases.
    # Parent-key flattening (RTLOG_ORIG_SYS, TRAN_SEQ_NO_NATURAL, TRAN_DATETIME) MUST
    # mirror sa_tran_head_marketplace.py exactly — drift = surrogate mismatch = orphans.
    flat = (
        microBatchDF
        .withColumn("item", explode(col("items")))
        .select(
            # --- parent key components (must match contract of tran_seq_no_expr) ---
            col("rtlog_orig_sys").alias("RTLOG_ORIG_SYS"),
            col("store_no").cast(LongType()).alias("STORE"),
            col("settle_date").cast(DateType()).alias("BUSINESS_DATE"),
            col("order_id").alias("TRAN_SEQ_NO_NATURAL"),
            col("settle_date").cast(TimestampType()).alias("TRAN_DATETIME"),

            # --- item line ---
            col("item.line_no").cast(IntegerType()).alias("ITEM_SEQ_NO"),
            col("item.sku").alias("ITEM"),
            lit("ITM").alias("ITEM_TYPE"),                       # decision 2: constant

            # Merch hierarchy — parse digits out of "D10" / "C101" / "S1001"
            regexp_extract(col("item.dept"),     r"\d+", 0).cast(IntegerType()).alias("DEPT"),
            regexp_extract(col("item.class"),    r"\d+", 0).cast(IntegerType()).alias("CLASS"),
            regexp_extract(col("item.subclass"), r"\d+", 0).cast(IntegerType()).alias("SUBCLASS"),

            # Quantity — decision 1: abs() flips RETURN/CREFUND negatives back positive
            abs(col("item.qty")).cast(DecimalType(12, 4)).alias("QTY"),

            # Price — decision 4: unit_price → UNIT_RETAIL
            col("item.unit_price").cast(DecimalType(20, 4)).alias("UNIT_RETAIL"),
        )
        .filter(
            col("STORE").isNotNull()
            & col("BUSINESS_DATE").isNotNull()
            & col("TRAN_SEQ_NO_NATURAL").isNotNull()
            & col("TRAN_DATETIME").isNotNull()
        )
    )

    # 2. Surrogate — shared helper. Channel-first hash → must match MKT head.
    keyed = flat.withColumn("TRAN_SEQ_NO", tran_seq_no_expr())

    # 3. FK enrich — shared helper. Single-key join, inherits CURRENCY_CODE + FX_RATE.
    enriched = enrich_with_parent_fx(keyed, PARENT_TABLE, ["TRAN_SEQ_NO"])

    # 4. Derive USD companions + lineage + explicit NULLs for ReSA columns absent in MKT.
    derived = (
        enriched
        # USD companions — TOTAL_IGTAX_AMT_USD will be NULL since IGTAX is NULL for MKT
        .withColumn("UNIT_RETAIL_USD",      (col("UNIT_RETAIL") * col("FX_RATE")).cast(DecimalType(20, 4)))
        .withColumn("ORIG_UNIT_RETAIL_USD", lit(None).cast(DecimalType(20, 4)))
        .withColumn("TOTAL_IGTAX_AMT_USD",  lit(None).cast(DecimalType(20, 4)))
        # 13 POS-only fields explicitly NULL'd (decision 5)
        .withColumn("ITEM_STATUS",          lit(None).cast(StringType()))
        .withColumn("ITEM_SWIPED_IND",      lit(None).cast(StringType()))
        .withColumn("DROP_SHIP_IND",        lit(None).cast(StringType()))
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
        # ReSA REF_NO5-8 — not populated by MKT bronze; explicit NULLs
        .withColumn("REF_NO5",              lit(None).cast(StringType()))
        .withColumn("REF_NO6",              lit(None).cast(StringType()))
        .withColumn("REF_NO7",              lit(None).cast(StringType()))
        .withColumn("REF_NO8",              lit(None).cast(StringType()))
        # Lineage
        .withColumn("_silver_ts", current_timestamp())
        .withColumn("_source",    lit(SOURCE_TABLE))
    )

    # 5. DQ split
    dq = derived.withColumn(
        "rejection_reason",
        array_compact(array(
            when(~col("_has_parent"),
                 lit("orphan_no_parent_header (TRAN_SEQ_NO not in sa_tran_head)")),
            when(col("ITEM_SEQ_NO").isNull(),
                 lit("ITEM_SEQ_NO null — cannot form PK")),
            when(col("ITEM_TYPE").isNull(),
                 lit("ITEM_TYPE null — mandatory in ReSA")),
        )),
    )

    clean   = dq.filter("size(rejection_reason) = 0").drop("rejection_reason", "_has_parent")
    rejects = dq.filter("size(rejection_reason) > 0").drop("_has_parent")
    # _quarantine_ts added inside merge_and_quarantine

    # Project clean to exact target schema order
    clean = clean.select(*TARGET_COLUMNS)

    # 6. MERGE clean + append rejects — shared helper. Standard whenMatchedUpdateAll is
    # correct here (unlike sa_store_day): sa_tran_item rows are channel-owned via the
    # channel-first surrogate, so there's no cross-writer overwrite risk.
    clean_n, reject_n = merge_and_quarantine(
        clean_df=clean,
        rejects_df=rejects,
        target_table=TARGET_TABLE,
        quarantine_table=QUARANTINE_TABLE,
        merge_keys=["TRAN_SEQ_NO", "ITEM_SEQ_NO"],
    )

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
# MAGIC ## Validation + diagnostics (channel-filtered to MKT)

# COMMAND ----------

total_mkt = spark.table(TARGET_TABLE).where("RTLOG_ORIG_SYS = 'MKT'").count()
total_pos = spark.table(TARGET_TABLE).where("RTLOG_ORIG_SYS = 'POS'").count()
total_all = spark.table(TARGET_TABLE).count()
print(f"silver.sa_tran_item MKT row count:   {total_mkt:,}")
print(f"silver.sa_tran_item POS row count:   {total_pos:,}")
print(f"silver.sa_tran_item total row count: {total_all:,}")

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

# Validation — only when MKT rows present
if total_mkt > 0:
    mkt = spark.table(TARGET_TABLE).where("RTLOG_ORIG_SYS = 'MKT'")

    # PK uniqueness on (TRAN_SEQ_NO, ITEM_SEQ_NO) within MKT
    dup_count = (
        mkt.groupBy("TRAN_SEQ_NO", "ITEM_SEQ_NO").count()
        .where("count > 1").count()
    )
    assert dup_count == 0, f"PK violation: {dup_count} duplicate (TRAN_SEQ_NO, ITEM_SEQ_NO) within MKT"
    print("PK uniqueness check passed (within MKT)")

    # FK integrity — every MKT item's TRAN_SEQ_NO must exist in sa_tran_head
    orphans = (
        mkt.select("TRAN_SEQ_NO").distinct().alias("i")
        .join(
            spark.table(PARENT_TABLE).where("RTLOG_ORIG_SYS = 'MKT'").select("TRAN_SEQ_NO").alias("h"),
            on="TRAN_SEQ_NO", how="left_anti",
        )
        .count()
    )
    assert orphans == 0, f"FK violation: {orphans} MKT item TRAN_SEQ_NO values with no parent header"
    print("FK integrity check passed (0 MKT orphans)")

    # Cross-channel collision check on (TRAN_SEQ_NO, ITEM_SEQ_NO)
    pos_keys = spark.table(TARGET_TABLE).where("RTLOG_ORIG_SYS = 'POS'").select("TRAN_SEQ_NO", "ITEM_SEQ_NO")
    mkt_keys = mkt.select("TRAN_SEQ_NO", "ITEM_SEQ_NO")
    overlap = pos_keys.intersect(mkt_keys).count()
    assert overlap == 0, f"Cross-channel PK collision: {overlap} shared (TRAN_SEQ_NO, ITEM_SEQ_NO)"
    print(f"Cross-channel collision check passed (POS ∩ MKT = ∅; {total_pos:,} POS + {total_mkt:,} MKT = {total_all:,})")

    # Items-per-order fan-out — should match the simulator's randint(1, 3)
    print("\n=== MKT items-per-order distribution ===")
    (
        mkt
        .groupBy("TRAN_SEQ_NO").count()
        .withColumnRenamed("count", "items_in_order")
        .groupBy("items_in_order").count()
        .withColumnRenamed("count", "orders")
        .orderBy("items_in_order")
        .show()
    )

    # QTY sanity — abs() flip should mean NO negative quantities anywhere on MKT
    neg_qty = mkt.where("QTY < 0").count()
    assert neg_qty == 0, f"abs() flip failed: {neg_qty} MKT items have negative QTY"
    print(f"QTY abs() flip check passed (0 negative MKT quantities)")

    # RETURN / CREFUND items should still have positive qty (direction is on the header)
    print("\n=== MKT QTY by parent TRAN_TYPE — all should be positive ===")
    (
        mkt.alias("i")
        .join(
            spark.table(PARENT_TABLE).where("RTLOG_ORIG_SYS = 'MKT'")
                 .select("TRAN_SEQ_NO", "TRAN_TYPE").alias("h"),
            on="TRAN_SEQ_NO", how="inner",
        )
        .groupBy("h.TRAN_TYPE")
        .agg(
            {"i.QTY": "min"},
        )
        .withColumnRenamed("min(QTY)", "min_qty")
        .show()
    )

    # FX sanity — first 10 MKT items across currencies
    print("=== FX sanity — first 10 MKT item lines ===")
    (
        mkt
        .select("STORE", "TRAN_SEQ_NO", "ITEM_SEQ_NO", "ITEM", "QTY", "UNIT_RETAIL",
                "CURRENCY_CODE", "FX_RATE", "UNIT_RETAIL_USD")
        .limit(10)
        .show(truncate=False)
    )

    # Reconciliation — MKT item rows should equal sum of items[] sizes in bronze
    bronze_items = (
        spark.table(SOURCE_TABLE)
        .selectExpr("size(items) as n_items")
        .agg({"n_items": "sum"})
        .collect()[0][0]
    )
    print(f"\nBronze items[] total elements: {bronze_items:,}")
    print(f"Silver MKT item rows:          {total_mkt:,}")
    print(f"Delta (rejects + filtered):    {bronze_items - total_mkt:,}")
