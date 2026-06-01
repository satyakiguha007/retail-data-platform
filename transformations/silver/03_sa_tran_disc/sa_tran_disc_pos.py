# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_tran_disc` (POS)
# MAGIC
# MAGIC Discount detail. One row per (item × discount), conformed to the
# MAGIC ReSA-canonical `SA_TRAN_DISC` schema. Child of `sa_tran_item` — and therefore
# MAGIC transitively of `sa_tran_head`.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source** | `retaildp.bronze.pos_rtlog` — `tran_disc` is an `ARRAY<STRUCT>` |
# MAGIC | **Target** | `retaildp.silver.sa_tran_disc` |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_tran_disc_rejects` |
# MAGIC | **FK lookup** | `silver.sa_tran_item` (line-level — transitively validates `sa_tran_head` and carries `CURRENCY_CODE` / `FX_RATE`) |
# MAGIC | **Pattern** | `readStream` + `availableNow` + `foreachBatch` → MERGE |
# MAGIC | **Idempotent** | Yes — deterministic `TRAN_SEQ_NO` + MERGE on 4-col composite PK |
# MAGIC | **Partitioned by** | `BUSINESS_DATE` (same as parents) |
# MAGIC
# MAGIC ## Shared helpers used (see `_shared/`)
# MAGIC - `surrogate_keys.tran_seq_no_expr()` — canonical `TRAN_SEQ_NO` hash (cell 6 step 3)
# MAGIC - `fx_helpers.enrich_with_parent_fx()` — line-level FK validate + inherit `CURRENCY_CODE` / `FX_RATE` (cell 6 step 4)
# MAGIC - `quarantine.merge_and_quarantine()` — idempotent MERGE + quarantine append (cell 6 step 7)
# MAGIC
# MAGIC ## Patterns introduced here (vs `02_sa_tran_item`)
# MAGIC 1. **Two-level FK collapsed to one join** — `sa_tran_disc.item_seq_no` points at a specific line in `sa_tran_item`, which itself points at a header. The `enrich_with_parent_fx` call with `join_keys=["TRAN_SEQ_NO", "ITEM_SEQ_NO"]` covers both relationships — if the item was quarantined upstream, or its header was, the discount has no parent and is rejected here.
# MAGIC 2. **Multi-column PK including a code column** — `(TRAN_SEQ_NO, ITEM_SEQ_NO, DISCOUNT_SEQ_NO, RMS_PROMO_TYPE)`. `RMS_PROMO_TYPE` is in the PK by ReSA design — the same `DISCOUNT_SEQ_NO` can repeat under different promo types (PROMO/MANUAL/COUPON/EMP), so the code column disambiguates.
# MAGIC
# MAGIC ## DQ rules (failures routed to quarantine)
# MAGIC 1. `orphan_no_parent_item` — `(TRAN_SEQ_NO, ITEM_SEQ_NO)` has no match in `silver.sa_tran_item`
# MAGIC 2. `ITEM_SEQ_NO` is NOT NULL (FK)
# MAGIC 3. `DISCOUNT_SEQ_NO` is NOT NULL (PK)
# MAGIC 4. `RMS_PROMO_TYPE` is NOT NULL (PK)
# MAGIC
# MAGIC ## NOT a DQ failure (kept as data, not quarantined)
# MAGIC - NULL `COUPON_NO`, NULL `PROMO_COMP` — typical for non-coupon PROMO discounts
# MAGIC - `ERROR_IND = 'Y'` — fault-injected rows pass through; Module 4 audit catches them
# MAGIC - `FX_RATE` null → `UNIT_DISCOUNT_AMT_USD` null (only if `fx_rates` has a gap)
# MAGIC - Transactions with empty `tran_disc` arrays simply produce 0 rows here (`explode` drops them)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, explode, current_timestamp, lit, when,
    array, array_compact,
)
from pyspark.sql.types import (
    StructType, StructField, LongType, IntegerType, StringType,
    DateType, TimestampType, DecimalType, ArrayType,
)

dbutils.widgets.text("source_table", "retaildp.bronze.pos_rtlog", "Source Bronze Table")
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
QUARANTINE_TABLE = "retaildp.quarantine.silver_sa_tran_disc_rejects"
PARENT_TABLE     = "retaildp.silver.sa_tran_item"   # line-level FK + CURRENCY_CODE / FX_RATE carrier
CHECKPOINT_PATH  = (
    "abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/silver/sa_tran_disc/"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema

# COMMAND ----------

sa_tran_disc_schema = StructType([
    # Composite PK
    StructField("TRAN_SEQ_NO",           LongType(),        nullable=False),
    StructField("ITEM_SEQ_NO",           IntegerType(),     nullable=False),
    StructField("DISCOUNT_SEQ_NO",       IntegerType(),     nullable=False),
    StructField("RMS_PROMO_TYPE",        StringType(),      nullable=False),
    StructField("RTLOG_ORIG_SYS",        StringType(),      nullable=False),

    # Parent-context natural columns
    StructField("STORE",                 LongType(),        nullable=False),
    StructField("BUSINESS_DATE",         DateType(),        nullable=False),

    # Discount attributes
    StructField("PROMOTION",             LongType(),        nullable=True),
    StructField("DISC_TYPE",             StringType(),      nullable=True),
    StructField("COUPON_NO",             StringType(),      nullable=True),
    StructField("QTY",                   DecimalType(12, 4), nullable=True),
    StructField("UNIT_DISCOUNT_AMT",     DecimalType(20, 4), nullable=True),
    StructField("UOM_QUANTITY",          DecimalType(12, 4), nullable=True),
    StructField("PROMO_COMP",            LongType(),        nullable=True),
    StructField("ERROR_IND",             StringType(),      nullable=True),

    # FX (inherited rate + USD companion)
    StructField("CURRENCY_CODE",         StringType(),      nullable=False),
    StructField("FX_RATE",               DecimalType(20, 6), nullable=True),
    StructField("UNIT_DISCOUNT_AMT_USD", DecimalType(20, 4), nullable=True),

    # Lineage
    StructField("_silver_ts",            TimestampType(),   nullable=False),
    StructField("_source",               StringType(),      nullable=False),
])

quarantine_schema = StructType(
    sa_tran_disc_schema.fields + [
        StructField("rejection_reason", ArrayType(StringType()), nullable=False),
        StructField("_quarantine_ts",   TimestampType(),         nullable=False),
    ]
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bootstrap target table if absent

# COMMAND ----------

if not spark.catalog.tableExists(TARGET_TABLE):
    print(f"Creating empty {TARGET_TABLE} partitioned by BUSINESS_DATE.")
    (
        spark.createDataFrame([], sa_tran_disc_schema).write
        .format("delta")
        .partitionBy("BUSINESS_DATE")
        .option("delta.autoOptimize.optimizeWrite", "true")
        .option("delta.autoOptimize.autoCompact",   "true")
        .saveAsTable(TARGET_TABLE)
    )
else:
    print(f"{TARGET_TABLE} already exists — skipping bootstrap.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## `merge_microbatch` — the heart

# COMMAND ----------

def merge_microbatch(microBatchDF: DataFrame, batch_id: int) -> None:
    # 1 + 2. Explode discounts, then flatten with explicit aliases
    flat = (
        microBatchDF
        .withColumn("disc", explode(col("tran_disc")))
        .select(
            # --- parent key components ---
            col("rtlog_orig_sys").alias("RTLOG_ORIG_SYS"),
            col("store").cast(LongType()).alias("STORE"),
            col("date").alias("BUSINESS_DATE"),
            col("tran_head.tran_seq_no").alias("TRAN_SEQ_NO_NATURAL"),
            col("tran_head.tran_datetime").cast(TimestampType()).alias("TRAN_DATETIME"),
            # --- discount line ---
            col("disc.item_seq_no").cast(IntegerType()).alias("ITEM_SEQ_NO"),
            col("disc.discount_seq_no").cast(IntegerType()).alias("DISCOUNT_SEQ_NO"),
            col("disc.rms_promo_type").alias("RMS_PROMO_TYPE"),
            col("disc.promotion").cast(LongType()).alias("PROMOTION"),
            col("disc.disc_type").alias("DISC_TYPE"),
            col("disc.coupon_no").alias("COUPON_NO"),
            col("disc.qty").cast(DecimalType(12, 4)).alias("QTY"),
            col("disc.unit_discount_amt").cast(DecimalType(20, 4)).alias("UNIT_DISCOUNT_AMT"),
            col("disc.uom_quantity").cast(DecimalType(12, 4)).alias("UOM_QUANTITY"),
            col("disc.promo_comp").cast(LongType()).alias("PROMO_COMP"),
            col("disc.error_ind").alias("ERROR_IND"),
        )
        .filter(
            col("STORE").isNotNull()
            & col("BUSINESS_DATE").isNotNull()
            & col("TRAN_SEQ_NO_NATURAL").isNotNull()
            & col("TRAN_DATETIME").isNotNull()
        )
    )

    # 3. Surrogate key — shared helper
    keyed = flat.withColumn("TRAN_SEQ_NO", tran_seq_no_expr())

    # 4. FK enrich — shared helper. Line-level: two join keys.
    enriched = enrich_with_parent_fx(keyed, PARENT_TABLE, ["TRAN_SEQ_NO", "ITEM_SEQ_NO"])

    # 5. Derive USD companion + lineage
    derived = (
        enriched
        .withColumn(
            "UNIT_DISCOUNT_AMT_USD",
            (col("UNIT_DISCOUNT_AMT") * col("FX_RATE")).cast(DecimalType(20, 4)),
        )
        .withColumn("_silver_ts", current_timestamp())
        .withColumn("_source",    lit(SOURCE_TABLE))
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
    # _quarantine_ts added by merge_and_quarantine

    # Project clean to exact target schema order
    clean = clean.select(*[f.name for f in sa_tran_disc_schema.fields])

    # 7. MERGE clean + append rejects — shared helper
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
# MAGIC ## Validation + diagnostics

# COMMAND ----------

silver_count = spark.table(TARGET_TABLE).count()
print(f"silver.sa_tran_disc row count: {silver_count}")

if spark.catalog.tableExists(QUARANTINE_TABLE):
    q_count = spark.table(QUARANTINE_TABLE).count()
    print(f"quarantine row count:          {q_count}")
    if q_count > 0:
        print("\nTop rejection reasons:")
        (
            spark.table(QUARANTINE_TABLE)
            .selectExpr("explode(rejection_reason) as reason")
            .groupBy("reason").count()
            .orderBy(col("count").desc())
            .show(truncate=False)
        )
else:
    print("quarantine row count:          0 (table not created)")

if silver_count > 0:
    dup_count = (
        spark.table(TARGET_TABLE)
        .groupBy("TRAN_SEQ_NO", "ITEM_SEQ_NO", "DISCOUNT_SEQ_NO", "RMS_PROMO_TYPE").count()
        .where("count > 1").count()
    )
    assert dup_count == 0, f"PK violation: {dup_count} duplicate composite keys"
    print("PK uniqueness check passed")

    orphans = (
        spark.table(TARGET_TABLE).select("TRAN_SEQ_NO", "ITEM_SEQ_NO").distinct().alias("d")
        .join(
            spark.table(PARENT_TABLE).select("TRAN_SEQ_NO", "ITEM_SEQ_NO").alias("i"),
            on=["TRAN_SEQ_NO", "ITEM_SEQ_NO"], how="left_anti",
        )
        .count()
    )
    assert orphans == 0, f"FK violation: {orphans} (TRAN_SEQ_NO, ITEM_SEQ_NO) pairs with no parent item line"
    print("FK integrity check passed (0 orphans)")

    print("\n=== RMS_PROMO_TYPE distribution ===")
    spark.table(TARGET_TABLE).groupBy("RMS_PROMO_TYPE").count().orderBy(col("count").desc()).show()

    print("=== DISC_TYPE distribution ===")
    spark.table(TARGET_TABLE).groupBy("DISC_TYPE").count().orderBy(col("count").desc()).show()

    print("=== Discounts per transaction (fan-out distribution) ===")
    (
        spark.table(TARGET_TABLE)
        .groupBy("TRAN_SEQ_NO").count()
        .withColumnRenamed("count", "discounts_in_tran")
        .groupBy("discounts_in_tran").count()
        .withColumnRenamed("count", "transactions")
        .orderBy("discounts_in_tran")
        .show()
    )

    print("=== Channel distribution (sanity — should all be POS in Pass-1) ===")
    spark.table(TARGET_TABLE).groupBy("RTLOG_ORIG_SYS").count().show()

    print("=== FX sanity — UNIT_DISCOUNT_AMT vs UNIT_DISCOUNT_AMT_USD for first 10 PROMO rows ===")
    (
        spark.table(TARGET_TABLE)
        .where("RMS_PROMO_TYPE = 'PROMO'")
        .select("STORE", "TRAN_SEQ_NO", "ITEM_SEQ_NO", "PROMOTION",
                "QTY", "UNIT_DISCOUNT_AMT", "CURRENCY_CODE", "FX_RATE", "UNIT_DISCOUNT_AMT_USD")
        .limit(10)
        .show(truncate=False)
    )
