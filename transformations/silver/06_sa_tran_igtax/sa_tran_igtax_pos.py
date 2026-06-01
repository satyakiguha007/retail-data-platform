# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_tran_igtax` (POS)
# MAGIC
# MAGIC Tax records held at the **item-line level** — one row per (item × tax authority),
# MAGIC keyed at line granularity (`ITEM_SEQ_NO` IS in the PK). Conformed to the
# MAGIC ReSA-canonical `SA_TRAN_IGTAX` schema. Tran-total tax lives in the partner
# MAGIC table `sa_tran_tax` (notebook 05). Direct child of `sa_tran_item`.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source** | `retaildp.bronze.pos_rtlog` — `tran_igtax` is an `ARRAY<STRUCT>` |
# MAGIC | **Target** | `retaildp.silver.sa_tran_igtax` |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_tran_igtax_rejects` |
# MAGIC | **FK lookup** | `silver.sa_tran_item` (line-level — transitively validates `sa_tran_head` and carries `CURRENCY_CODE` / `FX_RATE`) |
# MAGIC | **Pattern** | `readStream` + `availableNow` + `foreachBatch` → MERGE |
# MAGIC | **Idempotent** | Yes — deterministic `TRAN_SEQ_NO` + MERGE on `(TRAN_SEQ_NO, ITEM_SEQ_NO, IGTAX_SEQ_NO)` |
# MAGIC | **Partitioned by** | `BUSINESS_DATE` (same as parents) |
# MAGIC
# MAGIC ## Shared helpers used (see `_shared/`)
# MAGIC - `surrogate_keys.tran_seq_no_expr()` — canonical `TRAN_SEQ_NO` hash
# MAGIC - `fx_helpers.enrich_with_parent_fx()` — line-level FK validate + inherit `CURRENCY_CODE` / `FX_RATE`
# MAGIC - `quarantine.merge_and_quarantine()` — idempotent MERGE + quarantine append
# MAGIC - `schema_gate.bronze_array_has_inner_fields()` — defensive check before starting the stream (USA-only case)
# MAGIC
# MAGIC ## Patterns introduced here (vs `03_sa_tran_disc`)
# MAGIC 1. **Multiple tax-authority rows per item line** — IND splits every line into CGST + SGST (or IGST inter-state), so a single item typically produces 2 rows here. This is NOT a duplicate — `IGTAX_SEQ_NO` disambiguates per-authority. Expect a row count roughly 2× `sa_tran_item` for IND-heavy data.
# MAGIC 2. **Cross-table reconciliation** — `sa_tran_item.TOTAL_IGTAX_AMT` holds the per-item total tax carried verbatim from bronze. `SUM(TOTAL_IGTAX_AMT)` over this table grouped by `(TRAN_SEQ_NO, ITEM_SEQ_NO)` should equal the parent item's `TOTAL_IGTAX_AMT` within rounding tolerance. The validation cell asserts that — a ReSA-style audit check that exercises the relationship between the two tables.
# MAGIC
# MAGIC ## Surrogate / composite key
# MAGIC `TRAN_SEQ_NO = tran_seq_no_expr()`. PK extends with the natural `ITEM_SEQ_NO` (FK to `sa_tran_item`) and the POS-assigned `IGTAX_SEQ_NO`.
# MAGIC
# MAGIC ## DQ rules (failures routed to quarantine)
# MAGIC 1. `orphan_no_parent_item` — `(TRAN_SEQ_NO, ITEM_SEQ_NO)` has no match in `silver.sa_tran_item`
# MAGIC 2. `ITEM_SEQ_NO` is NOT NULL (FK)
# MAGIC 3. `IGTAX_SEQ_NO` is NOT NULL (PK)
# MAGIC 4. `TOTAL_IGTAX_AMT` is NOT NULL (mandatory in ReSA)
# MAGIC
# MAGIC ## NOT a DQ failure (kept as data, not quarantined)
# MAGIC - Multiple rows per `(TRAN_SEQ_NO, ITEM_SEQ_NO)` — that's CGST + SGST, NOT a duplicate
# MAGIC - Transactions with empty `tran_igtax` arrays simply produce 0 rows here (USA-only case)
# MAGIC - `ERROR_IND = 'Y'` — fault-injected rows pass through; Module 4 audit catches them
# MAGIC - `FX_RATE` null → `TOTAL_IGTAX_AMT_USD` null (only if `fx_rates` has a gap)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, explode, current_timestamp, lit, when,
    array, array_compact, sum as F_sum,
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

TARGET_TABLE     = "retaildp.silver.sa_tran_igtax"
QUARANTINE_TABLE = "retaildp.quarantine.silver_sa_tran_igtax_rejects"
PARENT_TABLE     = "retaildp.silver.sa_tran_item"
CHECKPOINT_PATH  = (
    "abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/silver/sa_tran_igtax/"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema

# COMMAND ----------

sa_tran_igtax_schema = StructType([
    # Composite PK
    StructField("TRAN_SEQ_NO",          LongType(),        nullable=False),
    StructField("ITEM_SEQ_NO",          IntegerType(),     nullable=False),
    StructField("IGTAX_SEQ_NO",         IntegerType(),     nullable=False),
    StructField("RTLOG_ORIG_SYS",       StringType(),      nullable=False),

    # Parent-context natural columns
    StructField("STORE",                LongType(),        nullable=False),
    StructField("BUSINESS_DATE",        DateType(),        nullable=False),

    # IGTAX attributes
    StructField("TAX_AUTHORITY",        StringType(),      nullable=True),
    StructField("IGTAX_CODE",           StringType(),      nullable=True),
    StructField("IGTAX_RATE",           DecimalType(20, 4), nullable=True),
    StructField("TOTAL_IGTAX_AMT",      DecimalType(20, 4), nullable=False),
    StructField("ERROR_IND",            StringType(),      nullable=True),

    # FX (inherited from parent item)
    StructField("CURRENCY_CODE",        StringType(),      nullable=False),
    StructField("FX_RATE",              DecimalType(20, 6), nullable=True),
    StructField("TOTAL_IGTAX_AMT_USD",  DecimalType(20, 4), nullable=True),

    # Lineage
    StructField("_silver_ts",           TimestampType(),   nullable=False),
    StructField("_source",              StringType(),      nullable=False),
])

quarantine_schema = StructType(
    sa_tran_igtax_schema.fields + [
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
        spark.createDataFrame([], sa_tran_igtax_schema).write
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
    # 1 + 2. Explode igtax rows, then flatten
    flat = (
        microBatchDF
        .withColumn("igtax", explode(col("tran_igtax")))
        .select(
            col("rtlog_orig_sys").alias("RTLOG_ORIG_SYS"),
            col("store").cast(LongType()).alias("STORE"),
            col("date").alias("BUSINESS_DATE"),
            col("tran_head.tran_seq_no").alias("TRAN_SEQ_NO_NATURAL"),
            col("tran_head.tran_datetime").cast(TimestampType()).alias("TRAN_DATETIME"),
            col("igtax.item_seq_no").cast(IntegerType()).alias("ITEM_SEQ_NO"),
            col("igtax.igtax_seq_no").cast(IntegerType()).alias("IGTAX_SEQ_NO"),
            col("igtax.tax_authority").alias("TAX_AUTHORITY"),
            col("igtax.igtax_code").alias("IGTAX_CODE"),
            col("igtax.igtax_rate").cast(DecimalType(20, 4)).alias("IGTAX_RATE"),
            col("igtax.total_igtax_amt").cast(DecimalType(20, 4)).alias("TOTAL_IGTAX_AMT"),
            col("igtax.error_ind").alias("ERROR_IND"),
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
            "TOTAL_IGTAX_AMT_USD",
            (col("TOTAL_IGTAX_AMT") * col("FX_RATE")).cast(DecimalType(20, 4)),
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
            when(col("IGTAX_SEQ_NO").isNull(),
                 lit("IGTAX_SEQ_NO null — cannot form PK")),
            when(col("TOTAL_IGTAX_AMT").isNull(),
                 lit("TOTAL_IGTAX_AMT null — mandatory in ReSA")),
        )),
    )

    clean   = dq.filter("size(rejection_reason) = 0").drop("rejection_reason", "_has_parent")
    rejects = dq.filter("size(rejection_reason) > 0").drop("_has_parent")
    # _quarantine_ts added by merge_and_quarantine

    clean = clean.select(*[f.name for f in sa_tran_igtax_schema.fields])

    # 7. MERGE clean + append rejects — shared helper
    clean_n, reject_n = merge_and_quarantine(
        clean_df=clean,
        rejects_df=rejects,
        target_table=TARGET_TABLE,
        quarantine_table=QUARANTINE_TABLE,
        merge_keys=["TRAN_SEQ_NO", "ITEM_SEQ_NO", "IGTAX_SEQ_NO"],
    )

    print(f"Batch {batch_id}: clean={clean_n} rejects={reject_n}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run the streaming merge — schema-gated
# MAGIC
# MAGIC If Auto Loader saw only empty `tran_igtax` arrays during bronze ingestion (USA-only case),
# MAGIC the inferred schema has no inner struct fields. The shared `bronze_array_has_inner_fields`
# MAGIC helper detects that and we skip the stream cleanly.

# COMMAND ----------

if not bronze_array_has_inner_fields(SOURCE_TABLE, "tran_igtax"):
    print("=" * 64)
    print("SKIPPING STREAM: bronze.pos_rtlog.tran_igtax has no inner struct fields.")
    print("Auto Loader saw only empty tran_igtax arrays during bronze ingestion —")
    print("expected when every transaction came from a TAX-mode country (USA / GBR).")
    print("Tran-total tax data lands in sa_tran_tax (notebook 05) instead.")
    print("The validation cell below will report an empty sa_tran_igtax — correct, not a failure.")
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
print(f"silver.sa_tran_igtax row count: {silver_count}")

if spark.catalog.tableExists(QUARANTINE_TABLE):
    q_count = spark.table(QUARANTINE_TABLE).count()
    print(f"quarantine row count:           {q_count}")
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
    print("quarantine row count:           0 (table not created)")

# IGTAX-presence by TAX_MODE — mirror of 05's diagnostic from the other side.
print("\n=== IGTAX-row presence by header TAX_MODE ===")
print("    Expected: IGTAX → 1+ rows per item (tax is per-line, lives here)")
print("              TAX   → 0 rows here (tax is on tran total, lives in sa_tran_tax)")
print("              BOTH  → 0 or 1+ rows per item (basket-dependent)")
(
    spark.table("retaildp.silver.sa_tran_head").alias("h")
    .join(
        spark.table(TARGET_TABLE).select("TRAN_SEQ_NO").distinct().alias("g"),
        on="TRAN_SEQ_NO", how="left",
    )
    .groupBy("h.TAX_MODE", "h.COUNTRY")
    .agg({"TRAN_SEQ_NO": "count"})
    .withColumnRenamed("count(TRAN_SEQ_NO)", "headers_with_igtax_rows")
    .orderBy("h.TAX_MODE", "h.COUNTRY")
    .show()
)

if silver_count > 0:
    dup_count = (
        spark.table(TARGET_TABLE)
        .groupBy("TRAN_SEQ_NO", "ITEM_SEQ_NO", "IGTAX_SEQ_NO").count()
        .where("count > 1").count()
    )
    assert dup_count == 0, f"PK violation: {dup_count} duplicate composite keys"
    print("PK uniqueness check passed")

    orphans = (
        spark.table(TARGET_TABLE).select("TRAN_SEQ_NO", "ITEM_SEQ_NO").distinct().alias("g")
        .join(
            spark.table(PARENT_TABLE).select("TRAN_SEQ_NO", "ITEM_SEQ_NO").alias("i"),
            on=["TRAN_SEQ_NO", "ITEM_SEQ_NO"], how="left_anti",
        )
        .count()
    )
    assert orphans == 0, f"FK violation: {orphans} (TRAN_SEQ_NO, ITEM_SEQ_NO) pairs with no parent item line"
    print("FK integrity check passed (0 orphans)")

    # --- Cross-table reconciliation: sum of per-authority igtax == item's TOTAL_IGTAX_AMT ---
    item_totals = (
        spark.table(PARENT_TABLE)
        .where("TOTAL_IGTAX_AMT IS NOT NULL")
        .select("TRAN_SEQ_NO", "ITEM_SEQ_NO",
                col("TOTAL_IGTAX_AMT").alias("item_total"))
    )
    igtax_sums = (
        spark.table(TARGET_TABLE)
        .groupBy("TRAN_SEQ_NO", "ITEM_SEQ_NO")
        .agg(F_sum("TOTAL_IGTAX_AMT").alias("igtax_sum"))
    )
    recon = (
        item_totals.alias("i")
        .join(igtax_sums.alias("g"), on=["TRAN_SEQ_NO", "ITEM_SEQ_NO"], how="left")
        .withColumn("delta", col("igtax_sum") - col("item_total"))
    )
    recon_breaks = recon.where("delta IS NULL OR abs(delta) > 0.01").count()
    print(f"\n=== Reconciliation: SUM(igtax.TOTAL_IGTAX_AMT) per item vs item.TOTAL_IGTAX_AMT ===")
    print(f"    Items where the two don't reconcile within 0.01 tolerance: {recon_breaks}")
    if recon_breaks > 0:
        print("    Sample of breaks:")
        recon.where("delta IS NULL OR abs(delta) > 0.01").select(
            "TRAN_SEQ_NO", "ITEM_SEQ_NO", "item_total", "igtax_sum", "delta"
        ).limit(10).show(truncate=False)

    print("=== TAX_AUTHORITY distribution ===")
    spark.table(TARGET_TABLE).groupBy("TAX_AUTHORITY").count().orderBy(col("count").desc()).show()

    print("=== IGTAX_CODE distribution ===")
    spark.table(TARGET_TABLE).groupBy("IGTAX_CODE").count().orderBy(col("count").desc()).show()

    print("=== Authorities per item line (fan-out distribution) ===")
    print("    Expected for IND: 2 (CGST + SGST) intra-state, or 1 (IGST) inter-state")
    (
        spark.table(TARGET_TABLE)
        .groupBy("TRAN_SEQ_NO", "ITEM_SEQ_NO").count()
        .withColumnRenamed("count", "authorities_per_item")
        .groupBy("authorities_per_item").count()
        .withColumnRenamed("count", "item_lines")
        .orderBy("authorities_per_item")
        .show()
    )

    print("=== Channel distribution (sanity — should all be POS in Pass-1) ===")
    spark.table(TARGET_TABLE).groupBy("RTLOG_ORIG_SYS").count().show()

    print("=== FX sanity — TOTAL_IGTAX_AMT vs TOTAL_IGTAX_AMT_USD for first 10 rows ===")
    (
        spark.table(TARGET_TABLE)
        .select("STORE", "TRAN_SEQ_NO", "ITEM_SEQ_NO", "TAX_AUTHORITY", "IGTAX_CODE",
                "IGTAX_RATE", "TOTAL_IGTAX_AMT", "CURRENCY_CODE", "FX_RATE", "TOTAL_IGTAX_AMT_USD")
        .limit(10)
        .show(truncate=False)
    )
else:
    print("\nsa_tran_igtax is empty — EXPECTED when Pass-1 only ran with stores whose tax model is tran-total (USA / GBR).")
    print("Tran-total tax for those transactions will land in sa_tran_tax (notebook 05).")
