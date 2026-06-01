# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_tran_tax` (POS)
# MAGIC
# MAGIC Tax records held at the **transaction-total level** — one row per tax line per transaction,
# MAGIC keyed at tran granularity (no `ITEM_SEQ_NO` in the PK). Conformed to the ReSA-canonical
# MAGIC `SA_TRAN_TAX` schema. Per-line tax lives in the partner table `sa_tran_igtax` (notebook 06).
# MAGIC Direct child of `sa_tran_head`.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source** | `retaildp.bronze.pos_rtlog` — `tran_tax` is an `ARRAY<STRUCT>` |
# MAGIC | **Target** | `retaildp.silver.sa_tran_tax` |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_tran_tax_rejects` |
# MAGIC | **FK lookup** | `silver.sa_tran_head` (header — carries `CURRENCY_CODE` / `FX_RATE` / `TAX_MODE`) |
# MAGIC | **Pattern** | `readStream` + `availableNow` + `foreachBatch` → MERGE |
# MAGIC | **Idempotent** | Yes — deterministic `TRAN_SEQ_NO` + MERGE on `(TRAN_SEQ_NO, TAX_SEQ_NO)` |
# MAGIC | **Partitioned by** | `BUSINESS_DATE` (same as parents) |
# MAGIC
# MAGIC ## Shared helpers used (see `_shared/`)
# MAGIC - `surrogate_keys.tran_seq_no_expr()` — canonical `TRAN_SEQ_NO` hash
# MAGIC - `fx_helpers.enrich_with_parent_fx()` — tran-level FK validate + inherit `CURRENCY_CODE` / `FX_RATE`
# MAGIC - `quarantine.merge_and_quarantine()` — idempotent MERGE + quarantine append
# MAGIC - `schema_gate.bronze_array_has_inner_fields()` — defensive check before starting the stream
# MAGIC
# MAGIC ## Patterns introduced here (vs `04_sa_tran_tender`)
# MAGIC 1. **PK at tran granularity** — `(TRAN_SEQ_NO, TAX_SEQ_NO)`. No `ITEM_SEQ_NO`. The PK shape *is* the structural distinction from `sa_tran_igtax`: tax that's computed on the transaction total (US/GB-style sales tax added at the register) lives here; tax that varies per SKU (IN GST baked into MRP, dual CGST+SGST per line) lives in `sa_tran_igtax` with `ITEM_SEQ_NO` in its PK.
# MAGIC 2. **The "may-be-empty" child** — fills only when the country's tax model puts tax at the transaction-total level. IND emits empty `tran_tax` arrays and produces 0 rows here. USA / GBR populate every transaction; UAE / SGP can populate either table or both.
# MAGIC 3. **Schema gate before the stream** — Auto Loader infers bronze schema from observed JSON. If every `tran_tax` array seen was empty (IND-only case), the inner struct has no fields and `col("tax.tax_seq_no")` won't resolve at planning time. The gate skips the stream cleanly in that case.
# MAGIC
# MAGIC The validation cell adds a **tax-presence-by-`TAX_MODE`** diagnostic that joins back to `sa_tran_head` to confirm the country → table-shape routing locked in by `01` is doing what it should:
# MAGIC - `TAX_MODE = IGTAX` headers → 0 rows here (tax is per-line, goes to `sa_tran_igtax`)
# MAGIC - `TAX_MODE = TAX`   headers → 1+ rows per header (tax on the tran total)
# MAGIC - `TAX_MODE = BOTH`  headers → 0 or 1+ rows per header (basket-dependent)
# MAGIC
# MAGIC ## DQ rules (failures routed to quarantine)
# MAGIC 1. `orphan_no_parent_header` — `TRAN_SEQ_NO` has no match in `silver.sa_tran_head`
# MAGIC 2. `TAX_SEQ_NO` is NOT NULL (PK)
# MAGIC 3. `TAX_CODE` is NOT NULL (mandatory in ReSA)
# MAGIC 4. `TAX_AMT` is NOT NULL (mandatory in ReSA)
# MAGIC
# MAGIC ## NOT a DQ failure (kept as data, not quarantined)
# MAGIC - Transactions with empty `tran_tax` arrays simply produce 0 rows here — **this is the normal case for IND**
# MAGIC - `ERROR_IND = 'Y'` — fault-injected rows pass through; Module 4 audit catches them
# MAGIC - `FX_RATE` null → `TAX_AMT_USD` null (only if `fx_rates` has a gap)

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

# MAGIC %run ../_shared/schema_gate

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

TARGET_TABLE     = "retaildp.silver.sa_tran_tax"
QUARANTINE_TABLE = "retaildp.quarantine.silver_sa_tran_tax_rejects"
PARENT_TABLE     = "retaildp.silver.sa_tran_head"
CHECKPOINT_PATH  = (
    "abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/silver/sa_tran_tax/"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema

# COMMAND ----------

sa_tran_tax_schema = StructType([
    # Composite PK
    StructField("TRAN_SEQ_NO",    LongType(),        nullable=False),
    StructField("TAX_SEQ_NO",     IntegerType(),     nullable=False),
    StructField("RTLOG_ORIG_SYS", StringType(),      nullable=False),

    # Parent-context natural columns
    StructField("STORE",          LongType(),        nullable=False),
    StructField("BUSINESS_DATE",  DateType(),        nullable=False),

    # Tax attributes
    StructField("TAX_CODE",       StringType(),      nullable=False),
    StructField("TAX_AMT",        DecimalType(20, 4), nullable=False),
    StructField("ERROR_IND",      StringType(),      nullable=True),

    # FX (inherited from header)
    StructField("CURRENCY_CODE",  StringType(),      nullable=False),
    StructField("FX_RATE",        DecimalType(20, 6), nullable=True),
    StructField("TAX_AMT_USD",    DecimalType(20, 4), nullable=True),

    # Lineage
    StructField("_silver_ts",     TimestampType(),   nullable=False),
    StructField("_source",        StringType(),      nullable=False),
])

quarantine_schema = StructType(
    sa_tran_tax_schema.fields + [
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
        spark.createDataFrame([], sa_tran_tax_schema).write
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
    # 1 + 2. Explode tax rows, then flatten
    flat = (
        microBatchDF
        .withColumn("tax", explode(col("tran_tax")))
        .select(
            col("rtlog_orig_sys").alias("RTLOG_ORIG_SYS"),
            col("store").cast(LongType()).alias("STORE"),
            col("date").alias("BUSINESS_DATE"),
            col("tran_head.tran_seq_no").alias("TRAN_SEQ_NO_NATURAL"),
            col("tran_head.tran_datetime").cast(TimestampType()).alias("TRAN_DATETIME"),
            col("tax.tax_seq_no").cast(IntegerType()).alias("TAX_SEQ_NO"),
            col("tax.tax_code").alias("TAX_CODE"),
            col("tax.tax_amt").cast(DecimalType(20, 4)).alias("TAX_AMT"),
            col("tax.error_ind").alias("ERROR_IND"),
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
        .withColumn("TAX_AMT_USD",
                    (col("TAX_AMT") * col("FX_RATE")).cast(DecimalType(20, 4)))
        .withColumn("_silver_ts", current_timestamp())
        .withColumn("_source",    lit(SOURCE_TABLE))
    )

    # 6. DQ split
    dq = derived.withColumn(
        "rejection_reason",
        array_compact(array(
            when(~col("_has_parent"),
                 lit("orphan_no_parent_header (TRAN_SEQ_NO not in sa_tran_head)")),
            when(col("TAX_SEQ_NO").isNull(),
                 lit("TAX_SEQ_NO null — cannot form PK")),
            when(col("TAX_CODE").isNull(),
                 lit("TAX_CODE null — mandatory in ReSA")),
            when(col("TAX_AMT").isNull(),
                 lit("TAX_AMT null — mandatory in ReSA")),
        )),
    )

    clean   = dq.filter("size(rejection_reason) = 0").drop("rejection_reason", "_has_parent")
    rejects = dq.filter("size(rejection_reason) > 0").drop("_has_parent")
    # _quarantine_ts added by merge_and_quarantine

    clean = clean.select(*[f.name for f in sa_tran_tax_schema.fields])

    # 7. MERGE clean + append rejects — shared helper
    clean_n, reject_n = merge_and_quarantine(
        clean_df=clean,
        rejects_df=rejects,
        target_table=TARGET_TABLE,
        quarantine_table=QUARANTINE_TABLE,
        merge_keys=["TRAN_SEQ_NO", "TAX_SEQ_NO"],
    )

    print(f"Batch {batch_id}: clean={clean_n} rejects={reject_n}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run the streaming merge — schema-gated
# MAGIC
# MAGIC If Auto Loader saw only empty `tran_tax` arrays during bronze ingestion (IND-only case),
# MAGIC the inferred schema has no inner struct fields. The shared `bronze_array_has_inner_fields`
# MAGIC helper detects that and we skip the stream cleanly.

# COMMAND ----------

if not bronze_array_has_inner_fields(SOURCE_TABLE, "tran_tax"):
    print("=" * 64)
    print("SKIPPING STREAM: bronze.pos_rtlog.tran_tax has no inner struct fields.")
    print("Auto Loader saw only empty tran_tax arrays during bronze ingestion —")
    print("expected when every transaction came from an IGTAX-mode country (IND).")
    print("Per-line tax data lands in sa_tran_igtax (notebook 06) instead.")
    print("The validation cell below will report an empty sa_tran_tax — correct, not a failure.")
    print("=" * 64)
else:
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
print(f"silver.sa_tran_tax row count: {silver_count}")

if spark.catalog.tableExists(QUARANTINE_TABLE):
    q_count = spark.table(QUARANTINE_TABLE).count()
    print(f"quarantine row count:         {q_count}")
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
    print("quarantine row count:         0 (table not created)")

# Tax-presence by TAX_MODE — the headline diagnostic for this notebook.
# Confirms 01's country → table-shape routing is doing what it should.
print("\n=== Tax-row presence by header TAX_MODE ===")
print("    Expected: IGTAX → 0 rows here (tax is per-line, goes to sa_tran_igtax)")
print("              TAX   → 1+ rows per header (tax on tran total)")
print("              BOTH  → 0 or 1+ rows per header (basket-dependent)")
(
    spark.table(PARENT_TABLE).alias("h")
    .join(
        spark.table(TARGET_TABLE).select("TRAN_SEQ_NO").alias("t"),
        on="TRAN_SEQ_NO", how="left",
    )
    .groupBy("h.TAX_MODE", "h.COUNTRY")
    .agg({"TRAN_SEQ_NO": "count"})
    .withColumnRenamed("count(TRAN_SEQ_NO)", "headers_with_tax_rows")
    .orderBy("h.TAX_MODE", "h.COUNTRY")
    .show()
)

if silver_count > 0:
    dup_count = (
        spark.table(TARGET_TABLE)
        .groupBy("TRAN_SEQ_NO", "TAX_SEQ_NO").count()
        .where("count > 1").count()
    )
    assert dup_count == 0, f"PK violation: {dup_count} duplicate (TRAN_SEQ_NO, TAX_SEQ_NO)"
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

    print("\n=== TAX_CODE distribution ===")
    spark.table(TARGET_TABLE).groupBy("TAX_CODE").count().orderBy(col("count").desc()).show()

    print("=== Tax rows per transaction (fan-out distribution) ===")
    (
        spark.table(TARGET_TABLE)
        .groupBy("TRAN_SEQ_NO").count()
        .withColumnRenamed("count", "tax_rows_in_tran")
        .groupBy("tax_rows_in_tran").count()
        .withColumnRenamed("count", "transactions")
        .orderBy("tax_rows_in_tran")
        .show()
    )

    print("=== FX sanity — TAX_AMT vs TAX_AMT_USD for first 10 rows ===")
    (
        spark.table(TARGET_TABLE)
        .select("STORE", "TRAN_SEQ_NO", "TAX_CODE", "TAX_AMT",
                "CURRENCY_CODE", "FX_RATE", "TAX_AMT_USD")
        .limit(10)
        .show(truncate=False)
    )
else:
    print("\nsa_tran_tax is empty — EXPECTED when Pass-1 only ran with stores whose tax model is per-line (IND).")
    print("Per-line tax for those transactions will land in sa_tran_igtax (notebook 06).")
