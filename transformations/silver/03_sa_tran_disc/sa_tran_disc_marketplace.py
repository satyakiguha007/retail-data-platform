# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_tran_disc` (Marketplace)
# MAGIC
# MAGIC Discount detail for marketplace orders. Where POS bronze emits a separate
# MAGIC `tran_disc[]` array, MKT bronze embeds a `discount_amt` field inside each
# MAGIC item — so the conformance pattern is *explode items[] → filter where
# MAGIC `discount_amt > 0`* rather than exploding a discount array directly.
# MAGIC One row per (MKT item × non-zero discount). Child of `sa_tran_item`,
# MAGIC transitively of `sa_tran_head`. Inherits `CURRENCY_CODE` + `FX_RATE` from
# MAGIC the item parent.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source** | `retaildp.bronze.marketplace` — `items[]` has inline `discount_amt` |
# MAGIC | **Target** | `retaildp.silver.sa_tran_disc` (shared with POS) |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_tran_disc_marketplace_rejects` (per-source) |
# MAGIC | **FK lookup** | `silver.sa_tran_item` (line-level — carries `CURRENCY_CODE` / `FX_RATE`) |
# MAGIC | **Pattern** | `readStream` + `availableNow` + `foreachBatch` → MERGE |
# MAGIC | **Idempotent** | Yes — deterministic surrogates + MERGE on 4-col composite PK |
# MAGIC | **Partitioned by** | `BUSINESS_DATE` (inherited from target) |
# MAGIC
# MAGIC ## Shared helpers used (see `_shared/`)
# MAGIC - `surrogate_keys.tran_seq_no_expr()` — same canonical hash as MKT head + items
# MAGIC - `fx_helpers.enrich_with_parent_fx()` — two-key FK validate against sa_tran_item
# MAGIC - `quarantine.merge_and_quarantine()` — standard `whenMatchedUpdateAll` (channel-owned)
# MAGIC
# MAGIC ## Locked decisions (Pass-2 design choices)
# MAGIC 1. **`DISCOUNT_SEQ_NO = 1` (constant)** — MKT items have at most one discount per line by
# MAGIC    simulator design (`if rng.random() < 0.40: promo = rng.choice(PROMOTIONS)`). One promo,
# MAGIC    one row. POS legitimately has multiple discounts per line (PROMO + COUPON stacked),
# MAGIC    so it uses a real seq_no from bronze; MKT doesn't need the disambiguation.
# MAGIC 2. **`RMS_PROMO_TYPE = 'PROMO'` (constant)** — all MKT discounts are promotional. The
# MAGIC    simulator's `PROMOTIONS` pool (`SALE10`, `SAVE15`, `FLASH20`, `DEAL25`, `BIGSALE`)
# MAGIC    are all promotional codes; there's no manual / employee / loyalty distinction.
# MAGIC 3. **`abs(qty)` matches item parent** — the discount QTY is the same as the item QTY
# MAGIC    (one item, one discount, same quantity). Same flip as `02_sa_tran_item`.
# MAGIC 4. **Filter on `discount_amt > 0`** — only items the simulator actually discounted (40%
# MAGIC    of lines) produce sa_tran_disc rows. Zero-discount items are not rejected; they
# MAGIC    simply don't fan out.
# MAGIC 5. **No bronze enrichment for promo metadata** — the simulator picks a promo but
# MAGIC    doesn't persist `promo_code` per item. `COUPON_NO`, `PROMOTION`, `DISC_TYPE`,
# MAGIC    `PROMO_COMP` are all NULL. Bronze data is the limit.
# MAGIC
# MAGIC ## Patterns introduced here (vs `sa_tran_disc_pos.py`)
# MAGIC 1. **Derivation, not explosion** — the discount "array" is conceptual: each item
# MAGIC    contributes 0 or 1 rows. Implemented as `explode(items) → filter` rather than
# MAGIC    `explode(tran_disc)`. Same downstream shape, different bronze provenance.
# MAGIC 2. **Constant PK component** — `DISCOUNT_SEQ_NO=1` is a constant for MKT but a real
# MAGIC    value for POS. The composite PK still works because POS uses MKT-disjoint
# MAGIC    `TRAN_SEQ_NO` (channel-first surrogate), so collisions are impossible even though
# MAGIC    POS row `(t1, i1, 1, 'PROMO')` and MKT row `(t1', i1, 1, 'PROMO')` exist side by side.
# MAGIC 3. **Per-source quarantine + checkpoint** — same Pass-2 convention as previous notebooks.
# MAGIC 4. **No bootstrap** — table exists from POS Pass-1.
# MAGIC
# MAGIC ## DQ rules (failures routed to quarantine)
# MAGIC 1. `orphan_no_parent_item` — `(TRAN_SEQ_NO, ITEM_SEQ_NO)` has no match in `silver.sa_tran_item`
# MAGIC    (should be ~0 since 02 MKT just landed cleanly)
# MAGIC 2. `ITEM_SEQ_NO` is NOT NULL (FK)
# MAGIC 3. `DISCOUNT_SEQ_NO` is NOT NULL (we hard-code 1 — defensive only)
# MAGIC 4. `RMS_PROMO_TYPE` is NOT NULL (we hard-code 'PROMO' — defensive only)
# MAGIC
# MAGIC ## NOT a DQ failure (kept as data, not quarantined)
# MAGIC - NULL `COUPON_NO` / `PROMOTION` / `DISC_TYPE` / `PROMO_COMP` — no MKT equivalent
# MAGIC - `FX_RATE` null → `UNIT_DISCOUNT_AMT_USD` null

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, explode, current_timestamp, lit, when,
    array, array_compact,
    abs,
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

TARGET_TABLE     = "retaildp.silver.sa_tran_disc"
QUARANTINE_TABLE = "retaildp.quarantine.silver_sa_tran_disc_marketplace_rejects"
PARENT_TABLE     = "retaildp.silver.sa_tran_item"   # line-level FK + CURRENCY_CODE / FX_RATE carrier
CHECKPOINT_PATH  = (
    "abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/silver/sa_tran_disc/marketplace/"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema — reused from existing table

# COMMAND ----------

assert spark.catalog.tableExists(TARGET_TABLE), (
    f"{TARGET_TABLE} does not exist. Run 03_sa_tran_disc/sa_tran_disc_pos.py first "
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
# MAGIC 1. **Explode + filter** — `explode(items)`, then `filter(item.discount_amt > 0)`.
# MAGIC    Only discounted items survive (~40% of lines per simulator).
# MAGIC 2. **Flatten** — parent-key components + line-no + discount_amt, with the two
# MAGIC    PK constants (`DISCOUNT_SEQ_NO=1`, `RMS_PROMO_TYPE='PROMO'`).
# MAGIC 3. **Surrogate** — `tran_seq_no_expr()`. Must match MKT head + items.
# MAGIC 4. **FK enrich** — `enrich_with_parent_fx()` with `["TRAN_SEQ_NO", "ITEM_SEQ_NO"]`.
# MAGIC    Validates against `sa_tran_item`; inherits `CURRENCY_CODE` + `FX_RATE`.
# MAGIC 5. **Derive + NULL POS-only columns** — `UNIT_DISCOUNT_AMT_USD`, lineage, and
# MAGIC    explicit NULLs for `PROMOTION`, `DISC_TYPE`, `COUPON_NO`, `UOM_QUANTITY`,
# MAGIC    `PROMO_COMP`, `ERROR_IND`.
# MAGIC 6. **DQ split** — clean vs reject via `rejection_reason` array.
# MAGIC 7. **Write** — `merge_and_quarantine()` on the 4-col composite PK.

# COMMAND ----------

def merge_microbatch(microBatchDF: DataFrame, batch_id: int) -> None:
    # 1 + 2. Explode items, filter to discounted ones only, then flatten.
    # Parent-key flattening MUST mirror sa_tran_head_marketplace + sa_tran_item_marketplace.
    flat = (
        microBatchDF
        .withColumn("item", explode(col("items")))
        .filter(col("item.discount_amt") > 0)                   # decision 4
        .select(
            # --- parent key components (must match contract of tran_seq_no_expr) ---
            col("rtlog_orig_sys").alias("RTLOG_ORIG_SYS"),
            col("store_no").cast(LongType()).alias("STORE"),
            col("settle_date").cast(DateType()).alias("BUSINESS_DATE"),
            col("order_id").alias("TRAN_SEQ_NO_NATURAL"),
            col("settle_date").cast(TimestampType()).alias("TRAN_DATETIME"),

            # --- discount line ---
            col("item.line_no").cast(IntegerType()).alias("ITEM_SEQ_NO"),
            lit(1).cast(IntegerType()).alias("DISCOUNT_SEQ_NO"),         # decision 1
            lit("PROMO").alias("RMS_PROMO_TYPE"),                        # decision 2

            # QTY: abs() to match the item parent's positive qty (decision 3)
            abs(col("item.qty")).cast(DecimalType(12, 4)).alias("QTY"),

            # Unit discount amount — already positive in bronze (simulator clamps)
            col("item.discount_amt").cast(DecimalType(20, 4)).alias("UNIT_DISCOUNT_AMT"),
        )
        .filter(
            col("STORE").isNotNull()
            & col("BUSINESS_DATE").isNotNull()
            & col("TRAN_SEQ_NO_NATURAL").isNotNull()
            & col("TRAN_DATETIME").isNotNull()
        )
    )

    # 3. Surrogate — shared helper. Channel-first hash → must match MKT head/items.
    keyed = flat.withColumn("TRAN_SEQ_NO", tran_seq_no_expr())

    # 4. FK enrich — shared helper. Two-key join validates the item parent.
    enriched = enrich_with_parent_fx(keyed, PARENT_TABLE, ["TRAN_SEQ_NO", "ITEM_SEQ_NO"])

    # 5. Derive USD companion + lineage + explicit NULLs for POS-only fields (decision 5)
    derived = (
        enriched
        .withColumn(
            "UNIT_DISCOUNT_AMT_USD",
            (col("UNIT_DISCOUNT_AMT") * col("FX_RATE")).cast(DecimalType(20, 4)),
        )
        # POS-only fields — no MKT equivalent
        .withColumn("PROMOTION",     lit(None).cast(LongType()))
        .withColumn("DISC_TYPE",     lit(None).cast(StringType()))
        .withColumn("COUPON_NO",     lit(None).cast(StringType()))
        .withColumn("UOM_QUANTITY",  lit(None).cast(DecimalType(12, 4)))
        .withColumn("PROMO_COMP",    lit(None).cast(LongType()))
        .withColumn("ERROR_IND",     lit(None).cast(StringType()))
        # Lineage
        .withColumn("_silver_ts",    current_timestamp())
        .withColumn("_source",       lit(SOURCE_TABLE))
    )

    # 6. DQ split
    dq = derived.withColumn(
        "rejection_reason",
        array_compact(array(
            when(~col("_has_parent"),
                 lit("orphan_no_parent_item ((TRAN_SEQ_NO, ITEM_SEQ_NO) not in sa_tran_item)")),
            when(col("ITEM_SEQ_NO").isNull(),
                 lit("ITEM_SEQ_NO null — cannot form FK")),
            when(col("DISCOUNT_SEQ_NO").isNull(),
                 lit("DISCOUNT_SEQ_NO null — cannot form PK")),
            when(col("RMS_PROMO_TYPE").isNull(),
                 lit("RMS_PROMO_TYPE null — cannot form PK")),
        )),
    )

    clean   = dq.filter("size(rejection_reason) = 0").drop("rejection_reason", "_has_parent")
    rejects = dq.filter("size(rejection_reason) > 0").drop("_has_parent")

    # Project clean to exact target schema order
    clean = clean.select(*TARGET_COLUMNS)

    # 7. MERGE clean + append rejects — shared helper.
    clean_n, reject_n = merge_and_quarantine(
        clean_df=clean,
        rejects_df=rejects,
        target_table=TARGET_TABLE,
        quarantine_table=QUARANTINE_TABLE,
        merge_keys=["TRAN_SEQ_NO", "ITEM_SEQ_NO", "DISCOUNT_SEQ_NO", "RMS_PROMO_TYPE"],
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
print(f"silver.sa_tran_disc MKT row count:   {total_mkt:,}")
print(f"silver.sa_tran_disc POS row count:   {total_pos:,}")
print(f"silver.sa_tran_disc total row count: {total_all:,}")

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

if total_mkt > 0:
    mkt = spark.table(TARGET_TABLE).where("RTLOG_ORIG_SYS = 'MKT'")

    # PK uniqueness within MKT (composite — 4 columns)
    dup_count = (
        mkt.groupBy("TRAN_SEQ_NO", "ITEM_SEQ_NO", "DISCOUNT_SEQ_NO", "RMS_PROMO_TYPE").count()
        .where("count > 1").count()
    )
    assert dup_count == 0, f"PK violation: {dup_count} duplicate composite keys within MKT"
    print("PK uniqueness check passed (within MKT)")

    # FK integrity — every MKT disc's (TRAN_SEQ_NO, ITEM_SEQ_NO) must exist in sa_tran_item MKT rows
    orphans = (
        mkt.select("TRAN_SEQ_NO", "ITEM_SEQ_NO").distinct().alias("d")
        .join(
            spark.table(PARENT_TABLE).where("RTLOG_ORIG_SYS = 'MKT'")
                 .select("TRAN_SEQ_NO", "ITEM_SEQ_NO").alias("i"),
            on=["TRAN_SEQ_NO", "ITEM_SEQ_NO"], how="left_anti",
        )
        .count()
    )
    assert orphans == 0, f"FK violation: {orphans} MKT (TRAN_SEQ_NO, ITEM_SEQ_NO) pairs with no parent item"
    print("FK integrity check passed (0 MKT orphans)")

    # Cross-channel PK collision (should be 0 — channel-first surrogate makes this impossible)
    pos_keys = (
        spark.table(TARGET_TABLE).where("RTLOG_ORIG_SYS = 'POS'")
        .select("TRAN_SEQ_NO", "ITEM_SEQ_NO", "DISCOUNT_SEQ_NO", "RMS_PROMO_TYPE")
    )
    mkt_keys = mkt.select("TRAN_SEQ_NO", "ITEM_SEQ_NO", "DISCOUNT_SEQ_NO", "RMS_PROMO_TYPE")
    overlap = pos_keys.intersect(mkt_keys).count()
    assert overlap == 0, f"Cross-channel PK collision: {overlap} shared 4-col keys"
    print(f"Cross-channel collision check passed (POS ∩ MKT = ∅; {total_pos:,} POS + {total_mkt:,} MKT = {total_all:,})")

    # MKT constants check — every MKT row should have DISCOUNT_SEQ_NO=1 and RMS_PROMO_TYPE='PROMO'
    bad_seq    = mkt.where("DISCOUNT_SEQ_NO != 1").count()
    bad_promo  = mkt.where("RMS_PROMO_TYPE != 'PROMO'").count()
    assert bad_seq == 0,   f"MKT constant violation: {bad_seq} rows with DISCOUNT_SEQ_NO != 1"
    assert bad_promo == 0, f"MKT constant violation: {bad_promo} rows with RMS_PROMO_TYPE != 'PROMO'"
    print("MKT constants check passed (all DISCOUNT_SEQ_NO=1, all RMS_PROMO_TYPE='PROMO')")

    # Discount-rate sanity — simulator promo probability is 0.40 per item
    parent_items_mkt = spark.table(PARENT_TABLE).where("RTLOG_ORIG_SYS = 'MKT'").count()
    discount_rate = total_mkt / parent_items_mkt if parent_items_mkt else 0.0
    print(f"\nDiscount rate: {total_mkt:,} disc / {parent_items_mkt:,} items = {discount_rate:.3f}")
    print(f"Expected ~0.40 (simulator's per-item promo probability)")

    # Distribution diagnostics
    print("\n=== MKT discount per item — should be exactly 1 for every discounted item ===")
    (
        mkt
        .groupBy("TRAN_SEQ_NO", "ITEM_SEQ_NO").count()
        .withColumnRenamed("count", "discs_per_item")
        .groupBy("discs_per_item").count()
        .withColumnRenamed("count", "items")
        .orderBy("discs_per_item")
        .show()
    )

    print("=== UNIT_DISCOUNT_AMT distribution by marketplace (via parent header REF_NO1) ===")
    (
        mkt.alias("d")
        .join(
            spark.table("retaildp.silver.sa_tran_head").where("RTLOG_ORIG_SYS = 'MKT'")
                 .select("TRAN_SEQ_NO", "REF_NO1").alias("h"),
            on="TRAN_SEQ_NO", how="inner",
        )
        .groupBy("h.REF_NO1")
        .agg(
            {"d.UNIT_DISCOUNT_AMT": "avg"},
        )
        .withColumnRenamed("avg(UNIT_DISCOUNT_AMT)", "avg_unit_disc")
        .orderBy("h.REF_NO1")
        .show()
    )

    print("=== FX sanity — first 10 MKT discount rows ===")
    (
        mkt
        .select("STORE", "TRAN_SEQ_NO", "ITEM_SEQ_NO", "QTY",
                "UNIT_DISCOUNT_AMT", "CURRENCY_CODE", "FX_RATE", "UNIT_DISCOUNT_AMT_USD")
        .limit(10)
        .show(truncate=False)
    )
