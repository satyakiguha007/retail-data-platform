# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_tran_tender` (Marketplace)
# MAGIC
# MAGIC Tender / payment detail for marketplace orders. Unlike POS — where each
# MAGIC transaction emits a `tran_tender[]` array of physical tender lines (cash,
# MAGIC card, voucher) — marketplace orders are settled by the marketplace itself.
# MAGIC There is no register-level tender authorization at the time of order;
# MAGIC the marketplace collects from the customer, deducts commission, and remits
# MAGIC the net to the seller. We model this as exactly **one synthesized tender
# MAGIC per order**, with `TENDER_TYPE_GROUP='MARKETPLACE'` — a new ReSA-extension
# MAGIC code alongside the POS-native CASH/CREDIT/DEBIT/VOUCHER/GIFTCARD.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source** | `retaildp.bronze.marketplace` (flat — no tender array to explode) |
# MAGIC | **Target** | `retaildp.silver.sa_tran_tender` (shared with POS) |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_tran_tender_marketplace_rejects` (per-source) |
# MAGIC | **FK lookup** | `silver.sa_tran_head` (tran-level — carries `CURRENCY_CODE` / `FX_RATE`) |
# MAGIC | **Pattern** | `readStream` + `availableNow` + `foreachBatch` → MERGE |
# MAGIC | **Idempotent** | Yes — deterministic surrogates + MERGE on `(TRAN_SEQ_NO, TENDER_SEQ_NO)` |
# MAGIC | **Partitioned by** | `BUSINESS_DATE` (inherited from target) |
# MAGIC
# MAGIC ## Shared helpers used (see `_shared/`)
# MAGIC - `surrogate_keys.tran_seq_no_expr()` — same canonical hash as MKT head + items + disc
# MAGIC - `fx_helpers.enrich_with_parent_fx()` — single-key FK validate against sa_tran_head
# MAGIC - `quarantine.merge_and_quarantine()` — standard `whenMatchedUpdateAll` (channel-owned)
# MAGIC
# MAGIC ## Locked decisions (Pass-2 design choices)
# MAGIC 1. **One tender per order — no explode** — bronze has no tender array. We synthesize
# MAGIC    a single row from the top-level `total_amt`. `TENDER_SEQ_NO = 1` constant.
# MAGIC 2. **`TENDER_TYPE_GROUP = 'MARKETPLACE'` (new code)** — ReSA-extension value. The
# MAGIC    physical settlement is opaque to us (Amazon/Flipkart/etc. handle the customer's
# MAGIC    card / wallet / COD internally); from our books, it's a single net remittance.
# MAGIC    `TENDER_TYPE_ID` left NULL — no SATT code_type entry for this in vanilla ReSA.
# MAGIC 3. **`TENDER_AMT = abs(total_amt)`** — bronze emits negative `total_amt` for
# MAGIC    RETURNED / CANCELLED_REFUND orders. Same convention as `sa_tran_head.VALUE` and
# MAGIC    `sa_tran_item.QTY`: store the **magnitude**, direction lives on the header's
# MAGIC    `TRAN_TYPE`. Yields the reconciliation invariant `TENDER_AMT == VALUE` per
# MAGIC    `TRAN_SEQ_NO` (single tender, full order amount).
# MAGIC 4. **No foreign-tender capture** — `ORIG_CURRENCY` / `ORIG_CURR_AMT` are NULL.
# MAGIC    Marketplace settlements are always in the storefront's local currency by
# MAGIC    design (e.g., Amazon_IN settles in INR even for foreign customers — currency
# MAGIC    conversion happens upstream of the seller).
# MAGIC 5. **All card-specific columns NULL** — `CC_NO`, `CC_AUTH_NO`, `CC_ENTRY_MODE`.
# MAGIC    No physical card data flows to the seller via marketplace settlement.
# MAGIC
# MAGIC ## Patterns introduced here (vs `sa_tran_tender_pos.py`)
# MAGIC 1. **Synthesis, not flattening** — no `explode` step; we project directly from the
# MAGIC    flat bronze row into a single tender. First MKT silver notebook to skip explode
# MAGIC    (all of head/item/disc had at least one).
# MAGIC 2. **Order-tender reconciliation check** — built into validation: for every MKT
# MAGIC    `TRAN_SEQ_NO`, `TENDER_AMT` must equal the header's `VALUE`. If this drifts,
# MAGIC    something's broken in the synthesis (e.g., we used `subtotal_amt` instead of
# MAGIC    `total_amt`, or forgot the `abs()` flip).
# MAGIC 3. **Per-source quarantine + checkpoint** — same Pass-2 convention.
# MAGIC 4. **No bootstrap** — table exists from POS Pass-1.
# MAGIC
# MAGIC ## DQ rules (failures routed to quarantine)
# MAGIC 1. `orphan_no_parent_header` — `TRAN_SEQ_NO` has no match in `silver.sa_tran_head`
# MAGIC    (should be ~0 since 01 MKT landed cleanly)
# MAGIC 2. `TENDER_SEQ_NO` is NOT NULL (we hard-code 1 — defensive)
# MAGIC 3. `TENDER_TYPE_GROUP` is NOT NULL (we hard-code 'MARKETPLACE' — defensive)
# MAGIC 4. `TENDER_AMT` is NOT NULL — catches simulator-fault rows with missing `total_amt`
# MAGIC
# MAGIC ## NOT a DQ failure (kept as data, not quarantined)
# MAGIC - NULL `CC_NO`, `CC_AUTH_NO`, `CC_ENTRY_MODE` (by design — no card data)
# MAGIC - NULL `VOUCHER_NO`, `ORIG_CURRENCY`, `ORIG_CURR_AMT` (by design)
# MAGIC - `FX_RATE` null → `TENDER_AMT_USD` null

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, current_timestamp, lit, when,
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

TARGET_TABLE     = "retaildp.silver.sa_tran_tender"
QUARANTINE_TABLE = "retaildp.quarantine.silver_sa_tran_tender_marketplace_rejects"
PARENT_TABLE     = "retaildp.silver.sa_tran_head"   # tran-level FK + CURRENCY_CODE / FX_RATE carrier
CHECKPOINT_PATH  = (
    "abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/silver/sa_tran_tender/marketplace/"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema — reused from existing table

# COMMAND ----------

assert spark.catalog.tableExists(TARGET_TABLE), (
    f"{TARGET_TABLE} does not exist. Run 04_sa_tran_tender/sa_tran_tender_pos.py first "
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
# MAGIC 1. **Synthesize** — project the flat bronze row into a single tender shape. No
# MAGIC    explode (decision 1). Parent-key fields MUST mirror sa_tran_head_marketplace.
# MAGIC 2. **Surrogate** — `tran_seq_no_expr()`. Same hash as MKT head — guarantees FK match.
# MAGIC 3. **FK enrich** — `enrich_with_parent_fx()` with `["TRAN_SEQ_NO"]`. Single-key join.
# MAGIC 4. **Derive + NULL POS-only columns** — `TENDER_AMT_USD`, lineage, and explicit NULLs
# MAGIC    for the 7 columns marketplace doesn't populate.
# MAGIC 5. **DQ split** — clean vs reject via `rejection_reason` array.
# MAGIC 6. **Write** — `merge_and_quarantine()` on `(TRAN_SEQ_NO, TENDER_SEQ_NO)`.

# COMMAND ----------

def merge_microbatch(microBatchDF: DataFrame, batch_id: int) -> None:
    # 1. Synthesize one tender row per bronze order (no explode — decision 1).
    # Parent-key flattening MUST mirror sa_tran_head_marketplace.py.
    flat = (
        microBatchDF
        .select(
            # --- parent key components ---
            col("rtlog_orig_sys").alias("RTLOG_ORIG_SYS"),
            col("store_no").cast(LongType()).alias("STORE"),
            col("settle_date").cast(DateType()).alias("BUSINESS_DATE"),
            col("order_id").alias("TRAN_SEQ_NO_NATURAL"),
            col("settle_date").cast(TimestampType()).alias("TRAN_DATETIME"),

            # --- synthesized tender (decisions 1, 2, 3) ---
            lit(1).cast(IntegerType()).alias("TENDER_SEQ_NO"),
            lit("MARKETPLACE").alias("TENDER_TYPE_GROUP"),
            abs(col("total_amt")).cast(DecimalType(20, 4)).alias("TENDER_AMT"),
        )
        .filter(
            col("STORE").isNotNull()
            & col("BUSINESS_DATE").isNotNull()
            & col("TRAN_SEQ_NO_NATURAL").isNotNull()
            & col("TRAN_DATETIME").isNotNull()
        )
    )

    # 2. Surrogate — shared helper. Channel-first hash → must match MKT head exactly.
    keyed = flat.withColumn("TRAN_SEQ_NO", tran_seq_no_expr())

    # 3. FK enrich — shared helper. Single-key join to sa_tran_head.
    enriched = enrich_with_parent_fx(keyed, PARENT_TABLE, ["TRAN_SEQ_NO"])

    # 4. Derive USD companion + lineage + explicit NULLs for POS-only fields (decisions 4, 5)
    derived = (
        enriched
        .withColumn(
            "TENDER_AMT_USD",
            (col("TENDER_AMT") * col("FX_RATE")).cast(DecimalType(20, 4)),
        )
        # POS-only — no MKT equivalent
        .withColumn("TENDER_TYPE_ID", lit(None).cast(IntegerType()))
        .withColumn("CC_NO",          lit(None).cast(StringType()))
        .withColumn("CC_AUTH_NO",     lit(None).cast(StringType()))
        .withColumn("CC_ENTRY_MODE",  lit(None).cast(StringType()))
        .withColumn("VOUCHER_NO",     lit(None).cast(StringType()))
        .withColumn("ORIG_CURRENCY",  lit(None).cast(StringType()))
        .withColumn("ORIG_CURR_AMT",  lit(None).cast(DecimalType(20, 4)))
        .withColumn("ERROR_IND",      lit(None).cast(StringType()))
        # Lineage
        .withColumn("_silver_ts",     current_timestamp())
        .withColumn("_source",        lit(SOURCE_TABLE))
    )

    # 5. DQ split
    dq = derived.withColumn(
        "rejection_reason",
        array_compact(array(
            when(~col("_has_parent"),
                 lit("orphan_no_parent_header (TRAN_SEQ_NO not in sa_tran_head)")),
            when(col("TENDER_SEQ_NO").isNull(),
                 lit("TENDER_SEQ_NO null — cannot form PK")),
            when(col("TENDER_TYPE_GROUP").isNull(),
                 lit("TENDER_TYPE_GROUP null — mandatory in ReSA")),
            when(col("TENDER_AMT").isNull(),
                 lit("TENDER_AMT null — mandatory in ReSA (likely missing bronze.total_amt)")),
        )),
    )

    clean   = dq.filter("size(rejection_reason) = 0").drop("rejection_reason", "_has_parent")
    rejects = dq.filter("size(rejection_reason) > 0").drop("_has_parent")

    # Project clean to exact target schema order
    clean = clean.select(*TARGET_COLUMNS)

    # 6. MERGE clean + append rejects — shared helper.
    clean_n, reject_n = merge_and_quarantine(
        clean_df=clean,
        rejects_df=rejects,
        target_table=TARGET_TABLE,
        quarantine_table=QUARANTINE_TABLE,
        merge_keys=["TRAN_SEQ_NO", "TENDER_SEQ_NO"],
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
print(f"silver.sa_tran_tender MKT row count:   {total_mkt:,}")
print(f"silver.sa_tran_tender POS row count:   {total_pos:,}")
print(f"silver.sa_tran_tender total row count: {total_all:,}")

if spark.catalog.tableExists(QUARANTINE_TABLE):
    q_count = spark.table(QUARANTINE_TABLE).count()
    print(f"MKT quarantine row count:              {q_count:,}")
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
    print("MKT quarantine row count:              0 (table not created)")

if total_mkt > 0:
    mkt = spark.table(TARGET_TABLE).where("RTLOG_ORIG_SYS = 'MKT'")

    # PK uniqueness within MKT
    dup_count = (
        mkt.groupBy("TRAN_SEQ_NO", "TENDER_SEQ_NO").count()
        .where("count > 1").count()
    )
    assert dup_count == 0, f"PK violation: {dup_count} duplicate (TRAN_SEQ_NO, TENDER_SEQ_NO) within MKT"
    print("PK uniqueness check passed (within MKT)")

    # FK integrity — every MKT tender's TRAN_SEQ_NO must exist in sa_tran_head MKT rows
    orphans = (
        mkt.select("TRAN_SEQ_NO").distinct().alias("t")
        .join(
            spark.table(PARENT_TABLE).where("RTLOG_ORIG_SYS = 'MKT'").select("TRAN_SEQ_NO").alias("h"),
            on="TRAN_SEQ_NO", how="left_anti",
        )
        .count()
    )
    assert orphans == 0, f"FK violation: {orphans} MKT TRAN_SEQ_NO values with no parent header"
    print("FK integrity check passed (0 MKT orphans)")

    # Cross-channel PK collision
    pos_keys = spark.table(TARGET_TABLE).where("RTLOG_ORIG_SYS = 'POS'").select("TRAN_SEQ_NO", "TENDER_SEQ_NO")
    mkt_keys = mkt.select("TRAN_SEQ_NO", "TENDER_SEQ_NO")
    overlap = pos_keys.intersect(mkt_keys).count()
    assert overlap == 0, f"Cross-channel PK collision: {overlap} shared (TRAN_SEQ_NO, TENDER_SEQ_NO)"
    print(f"Cross-channel collision check passed (POS ∩ MKT = ∅; {total_pos:,} POS + {total_mkt:,} MKT = {total_all:,})")

    # MKT constants — every MKT row should have TENDER_SEQ_NO=1 and TENDER_TYPE_GROUP='MARKETPLACE'
    bad_seq   = mkt.where("TENDER_SEQ_NO != 1").count()
    bad_group = mkt.where("TENDER_TYPE_GROUP != 'MARKETPLACE'").count()
    assert bad_seq == 0,   f"MKT constant violation: {bad_seq} rows with TENDER_SEQ_NO != 1"
    assert bad_group == 0, f"MKT constant violation: {bad_group} rows with TENDER_TYPE_GROUP != 'MARKETPLACE'"
    print("MKT constants check passed (all TENDER_SEQ_NO=1, all TENDER_TYPE_GROUP='MARKETPLACE')")

    # One-tender-per-order invariant
    multi_tender_orders = (
        mkt.groupBy("TRAN_SEQ_NO").count()
        .where("count > 1").count()
    )
    assert multi_tender_orders == 0, f"Invariant violation: {multi_tender_orders} MKT orders have multiple tenders"
    print("One-tender-per-MKT-order invariant check passed")

    # Order-tender reconciliation — TENDER_AMT must equal sa_tran_head.VALUE per TRAN_SEQ_NO
    mismatched = (
        mkt.alias("t")
        .join(
            spark.table(PARENT_TABLE).where("RTLOG_ORIG_SYS = 'MKT'")
                 .select("TRAN_SEQ_NO", "VALUE").alias("h"),
            on="TRAN_SEQ_NO", how="inner",
        )
        .where("abs(t.TENDER_AMT - h.VALUE) > 0.01")
        .count()
    )
    assert mismatched == 0, (
        f"Reconciliation violation: {mismatched} MKT orders where TENDER_AMT != header.VALUE "
        "(>0.01 tolerance for decimal rounding)"
    )
    print("Order-tender reconciliation passed (TENDER_AMT == sa_tran_head.VALUE for all MKT orders)")

    # MKT tender count should equal MKT order count (1:1)
    mkt_header_count = spark.table(PARENT_TABLE).where("RTLOG_ORIG_SYS = 'MKT'").count()
    print(f"\nMKT header count:   {mkt_header_count:,}")
    print(f"MKT tender count:   {total_mkt:,}")
    print(f"Should be equal (1:1)")
    assert mkt_header_count == total_mkt, (
        f"1:1 invariant violated: {mkt_header_count} headers vs {total_mkt} tenders"
    )

    # Distribution diagnostics
    print("\n=== MKT TENDER_TYPE_GROUP distribution (should be all 'MARKETPLACE') ===")
    mkt.groupBy("TENDER_TYPE_GROUP").count().show()

    print("=== TENDER_AMT distribution by currency ===")
    (
        mkt.groupBy("CURRENCY_CODE")
        .agg({"TENDER_AMT": "avg", "TENDER_AMT_USD": "avg"})
        .withColumnRenamed("avg(TENDER_AMT)", "avg_tender_amt")
        .withColumnRenamed("avg(TENDER_AMT_USD)", "avg_tender_amt_usd")
        .orderBy("CURRENCY_CODE")
        .show()
    )

    print("=== TENDER_AMT distribution by parent TRAN_TYPE (sanity — all positive) ===")
    (
        mkt.alias("t")
        .join(
            spark.table(PARENT_TABLE).where("RTLOG_ORIG_SYS = 'MKT'")
                 .select("TRAN_SEQ_NO", "TRAN_TYPE").alias("h"),
            on="TRAN_SEQ_NO", how="inner",
        )
        .groupBy("h.TRAN_TYPE")
        .agg({"t.TENDER_AMT": "min", "t.TENDER_AMT": "avg"})
        .show()
    )

    print("=== FX sanity — first 10 MKT tender rows ===")
    (
        mkt
        .select("STORE", "TRAN_SEQ_NO", "TENDER_TYPE_GROUP", "TENDER_AMT",
                "CURRENCY_CODE", "FX_RATE", "TENDER_AMT_USD")
        .limit(10)
        .show(truncate=False)
    )
