# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_tran_tender` (POS)
# MAGIC
# MAGIC Tender / payment detail. One row per tender used in the transaction, conformed to
# MAGIC the ReSA-canonical `SA_TRAN_TENDER` schema. Direct child of `sa_tran_head`.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source** | `retaildp.bronze.pos_rtlog` — `tran_tender` is an `ARRAY<STRUCT>` |
# MAGIC | **Target** | `retaildp.silver.sa_tran_tender` |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_tran_tender_rejects` |
# MAGIC | **FK lookup** | `silver.sa_tran_head` (header — carries `CURRENCY_CODE` / `FX_RATE`) |
# MAGIC | **Pattern** | `readStream` + `availableNow` + `foreachBatch` → MERGE |
# MAGIC | **Idempotent** | Yes — deterministic `TRAN_SEQ_NO` + MERGE on `(TRAN_SEQ_NO, TENDER_SEQ_NO)` |
# MAGIC | **Partitioned by** | `BUSINESS_DATE` (same as parents) |
# MAGIC
# MAGIC ## Shared helpers used (see `_shared/`)
# MAGIC - `surrogate_keys.tran_seq_no_expr()` — canonical `TRAN_SEQ_NO` hash
# MAGIC - `fx_helpers.enrich_with_parent_fx()` — tran-level FK validate + inherit `CURRENCY_CODE` / `FX_RATE`
# MAGIC - `quarantine.merge_and_quarantine()` — idempotent MERGE + quarantine append
# MAGIC
# MAGIC ## Patterns introduced here (vs `03_sa_tran_disc`)
# MAGIC 1. **Back to single-level FK** — tenders are tran-level (not line-level), so `enrich_with_parent_fx` is called with `join_keys=["TRAN_SEQ_NO"]` — same shape as `02_sa_tran_item`. Item-level children (`disc`, `igtax`) need the line; tran-level children (`tender`, `tax`) don't.
# MAGIC 2. **Multi-currency capture** — a tender carries its own `ORIG_CURRENCY` + `ORIG_CURR_AMT` that *can* differ from the transaction's `CURRENCY_CODE` (e.g., a foreign card paying in a local store). We keep both:
# MAGIC    - `ORIG_CURRENCY` / `ORIG_CURR_AMT` — what the tender actually was
# MAGIC    - `CURRENCY_CODE` (inherited from header) — what the transaction was settled in
# MAGIC    - `TENDER_AMT_USD = TENDER_AMT * FX_RATE` — normalised via the **tran's** FX rate, so totals reconcile with the header's `VALUE_USD`
# MAGIC    A diagnostic in cell 8 flags any drift between the two — interesting once marketplace cross-border orders arrive in Pass-2.
# MAGIC
# MAGIC ## DQ rules (failures routed to quarantine)
# MAGIC 1. `orphan_no_parent_header` — `TRAN_SEQ_NO` has no match in `silver.sa_tran_head`
# MAGIC 2. `TENDER_SEQ_NO` is NOT NULL (PK)
# MAGIC 3. `TENDER_TYPE_GROUP` is NOT NULL (mandatory in ReSA)
# MAGIC 4. `TENDER_AMT` is NOT NULL (mandatory in ReSA — every tender must declare an amount)
# MAGIC
# MAGIC ## NOT a DQ failure (kept as data, not quarantined)
# MAGIC - NULL `CC_NO` for non-card tenders (CASH/VOUCHER legitimately have none)
# MAGIC - NULL `VOUCHER_NO`, NULL `CC_AUTH_NO` for non-applicable tender types
# MAGIC - `ORIG_CURRENCY != CURRENCY_CODE` — legitimate foreign tender (won't happen in Pass-1)
# MAGIC - `ERROR_IND = 'Y'` — fault-injected rows pass through; Module 4 audit catches them
# MAGIC - `FX_RATE` null → `TENDER_AMT_USD` null (only if `fx_rates` has a gap)
# MAGIC
# MAGIC ## PII note
# MAGIC `CC_NO` arrives already-masked from the POS simulator (`************4521` shape). Silver carries it through as-is.

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

TARGET_TABLE     = "retaildp.silver.sa_tran_tender"
QUARANTINE_TABLE = "retaildp.quarantine.silver_sa_tran_tender_rejects"
PARENT_TABLE     = "retaildp.silver.sa_tran_head"   # tran-level FK + CURRENCY_CODE / FX_RATE carrier
CHECKPOINT_PATH  = (
    "abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/silver/sa_tran_tender/"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema

# COMMAND ----------

sa_tran_tender_schema = StructType([
    # Composite PK
    StructField("TRAN_SEQ_NO",       LongType(),        nullable=False),
    StructField("TENDER_SEQ_NO",     IntegerType(),     nullable=False),
    StructField("RTLOG_ORIG_SYS",    StringType(),      nullable=False),

    # Parent-context natural columns
    StructField("STORE",             LongType(),        nullable=False),
    StructField("BUSINESS_DATE",     DateType(),        nullable=False),

    # Tender attributes
    StructField("TENDER_TYPE_GROUP", StringType(),      nullable=False),
    StructField("TENDER_TYPE_ID",    IntegerType(),     nullable=True),
    StructField("TENDER_AMT",        DecimalType(20, 4), nullable=False),

    # Card-specific (NULL for non-card)
    StructField("CC_NO",             StringType(),      nullable=True),
    StructField("CC_AUTH_NO",        StringType(),      nullable=True),
    StructField("CC_ENTRY_MODE",     StringType(),      nullable=True),

    # Voucher / other
    StructField("VOUCHER_NO",        StringType(),      nullable=True),

    # Multi-currency capture
    StructField("ORIG_CURRENCY",     StringType(),      nullable=True),
    StructField("ORIG_CURR_AMT",     DecimalType(20, 4), nullable=True),

    # Flag
    StructField("ERROR_IND",         StringType(),      nullable=True),

    # FX (inherited from header — tran's currency, not tender's)
    StructField("CURRENCY_CODE",     StringType(),      nullable=False),
    StructField("FX_RATE",           DecimalType(20, 6), nullable=True),
    StructField("TENDER_AMT_USD",    DecimalType(20, 4), nullable=True),

    # Lineage
    StructField("_silver_ts",        TimestampType(),   nullable=False),
    StructField("_source",           StringType(),      nullable=False),
])

quarantine_schema = StructType(
    sa_tran_tender_schema.fields + [
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
        spark.createDataFrame([], sa_tran_tender_schema).write
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
    # 1 + 2. Explode tenders, then flatten
    flat = (
        microBatchDF
        .withColumn("tender", explode(col("tran_tender")))
        .select(
            # --- parent key components ---
            col("rtlog_orig_sys").alias("RTLOG_ORIG_SYS"),
            col("store").cast(LongType()).alias("STORE"),
            col("date").alias("BUSINESS_DATE"),
            col("tran_head.tran_seq_no").alias("TRAN_SEQ_NO_NATURAL"),
            col("tran_head.tran_datetime").cast(TimestampType()).alias("TRAN_DATETIME"),
            # --- tender line ---
            col("tender.tender_seq_no").cast(IntegerType()).alias("TENDER_SEQ_NO"),
            col("tender.tender_type_group").alias("TENDER_TYPE_GROUP"),
            col("tender.tender_type_id").cast(IntegerType()).alias("TENDER_TYPE_ID"),
            col("tender.tender_amt").cast(DecimalType(20, 4)).alias("TENDER_AMT"),
            col("tender.cc_no").alias("CC_NO"),
            col("tender.cc_auth_no").alias("CC_AUTH_NO"),
            col("tender.cc_entry_mode").alias("CC_ENTRY_MODE"),
            col("tender.voucher_no").alias("VOUCHER_NO"),
            col("tender.orig_currency").alias("ORIG_CURRENCY"),
            col("tender.orig_curr_amt").cast(DecimalType(20, 4)).alias("ORIG_CURR_AMT"),
            col("tender.error_ind").alias("ERROR_IND"),
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

    # 4. FK enrich — shared helper. Tran-level: single join key.
    enriched = enrich_with_parent_fx(keyed, PARENT_TABLE, ["TRAN_SEQ_NO"])

    # 5. Derive USD companion + lineage
    derived = (
        enriched
        .withColumn(
            "TENDER_AMT_USD",
            (col("TENDER_AMT") * col("FX_RATE")).cast(DecimalType(20, 4)),
        )
        .withColumn("_silver_ts", current_timestamp())
        .withColumn("_source",    lit(SOURCE_TABLE))
    )

    # 6. DQ split
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
                 lit("TENDER_AMT null — mandatory in ReSA")),
        )),
    )

    clean   = dq.filter("size(rejection_reason) = 0").drop("rejection_reason", "_has_parent")
    rejects = dq.filter("size(rejection_reason) > 0").drop("_has_parent")
    # _quarantine_ts added by merge_and_quarantine

    clean = clean.select(*[f.name for f in sa_tran_tender_schema.fields])

    # 7. MERGE clean + append rejects — shared helper
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
# MAGIC ## Validation + diagnostics

# COMMAND ----------

silver_count = spark.table(TARGET_TABLE).count()
print(f"silver.sa_tran_tender row count: {silver_count}")

if spark.catalog.tableExists(QUARANTINE_TABLE):
    q_count = spark.table(QUARANTINE_TABLE).count()
    print(f"quarantine row count:            {q_count}")
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
    print("quarantine row count:            0 (table not created)")

if silver_count > 0:
    dup_count = (
        spark.table(TARGET_TABLE)
        .groupBy("TRAN_SEQ_NO", "TENDER_SEQ_NO").count()
        .where("count > 1").count()
    )
    assert dup_count == 0, f"PK violation: {dup_count} duplicate (TRAN_SEQ_NO, TENDER_SEQ_NO)"
    print("PK uniqueness check passed")

    orphans = (
        spark.table(TARGET_TABLE).select("TRAN_SEQ_NO").distinct().alias("t")
        .join(
            spark.table(PARENT_TABLE).select("TRAN_SEQ_NO").alias("h"),
            on="TRAN_SEQ_NO", how="left_anti",
        )
        .count()
    )
    assert orphans == 0, f"FK violation: {orphans} TRAN_SEQ_NO values with no parent header"
    print("FK integrity check passed (0 orphans)")

    print("\n=== TENDER_TYPE_GROUP distribution ===")
    spark.table(TARGET_TABLE).groupBy("TENDER_TYPE_GROUP").count().orderBy(col("count").desc()).show()

    print("=== CC_ENTRY_MODE distribution (CARD tenders only) ===")
    (
        spark.table(TARGET_TABLE)
        .where("TENDER_TYPE_GROUP = 'CARD'")
        .groupBy("CC_ENTRY_MODE").count()
        .orderBy(col("count").desc())
        .show()
    )

    print("=== Tenders per transaction (fan-out distribution) ===")
    (
        spark.table(TARGET_TABLE)
        .groupBy("TRAN_SEQ_NO").count()
        .withColumnRenamed("count", "tenders_in_tran")
        .groupBy("tenders_in_tran").count()
        .withColumnRenamed("count", "transactions")
        .orderBy("tenders_in_tran")
        .show()
    )

    print("=== Channel distribution (sanity — should all be POS in Pass-1) ===")
    spark.table(TARGET_TABLE).groupBy("RTLOG_ORIG_SYS").count().show()

    fx_drift = (
        spark.table(TARGET_TABLE)
        .where("ORIG_CURRENCY IS NOT NULL AND ORIG_CURRENCY != CURRENCY_CODE")
        .count()
    )
    print(f"\n=== Multi-currency drift (ORIG_CURRENCY != CURRENCY_CODE): {fx_drift} ===")
    print("    (expected 0 in Pass-1; non-zero means foreign tenders — interesting for Module 4)")

    print("\n=== FX sanity — TENDER_AMT vs TENDER_AMT_USD for first 10 CARD rows ===")
    (
        spark.table(TARGET_TABLE)
        .where("TENDER_TYPE_GROUP = 'CARD'")
        .select("STORE", "TRAN_SEQ_NO", "TENDER_TYPE_GROUP", "TENDER_AMT",
                "CURRENCY_CODE", "ORIG_CURRENCY", "FX_RATE", "TENDER_AMT_USD")
        .limit(10)
        .show(truncate=False)
    )
