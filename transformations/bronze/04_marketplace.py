# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze: Marketplace Feeds (Auto Loader)
# MAGIC
# MAGIC **Source:** NDJSON at `abfss://raw@.../marketplace/marketplace=*/date=*/feed.ndjson`
# MAGIC **Target:** `retaildp.bronze.marketplace` — managed Delta in bronze container
# MAGIC **Pattern:** Structured Streaming + Auto Loader (`cloudFiles`) + `trigger(availableNow=True)`
# MAGIC
# MAGIC Last of the 5 Bronze notebooks. Follows the same Auto Loader pattern as `03_pos_rtlog.py`
# MAGIC but with flatter JSON and a 2-level partition layout.
# MAGIC
# MAGIC ## Channel marker
# MAGIC
# MAGIC Marketplace orders carry `rtlog_orig_sys = 'MKT'` (vs `'POS'` for tills). Silver uses this
# MAGIC marker to differentiate sources when conforming to the ReSA `SA_TRAN_*` canonical model.
# MAGIC All channels land in the same Silver tables.
# MAGIC
# MAGIC ## Lessons learned baked in
# MAGIC
# MAGIC - No `CLUSTER BY` at Bronze (Delta stats-schema limit on nested data)
# MAGIC - No pre-create — writeStream creates the table from inferred schema
# MAGIC - `_metadata.file_path` for lineage (UC serverless blocks `input_file_name()`)
# MAGIC - Properties + comment applied via ALTER post-write
# MAGIC - `_rescued_data` column to catch any schema drift

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration

# COMMAND ----------

CATALOG = "retaildp"
SCHEMA  = "bronze"
TABLE   = "marketplace"
TARGET_TABLE = f"{CATALOG}.{SCHEMA}.{TABLE}"

# Source — marketplace NDJSON feeds dropped by the marketplace simulator
RAW_BASE     = "abfss://raw@stretaildpsatyaki01.dfs.core.windows.net/"
RAW_MKT_PATH = RAW_BASE + "marketplace/"

# Checkpoints — Auto Loader file-tracking state + inferred schema
CHECKPOINTS_BASE    = "abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/"
SCHEMA_LOCATION     = CHECKPOINTS_BASE + "marketplace/schema/"
CHECKPOINT_LOCATION = CHECKPOINTS_BASE + "marketplace/state/"

spark.sql(f"USE CATALOG {CATALOG}")
print(f"Target table:        {TARGET_TABLE}")
print(f"Source path:         {RAW_MKT_PATH}")
print(f"Schema location:     {SCHEMA_LOCATION}")
print(f"Checkpoint location: {CHECKPOINT_LOCATION}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Auto Loader read stream

# COMMAND ----------

from pyspark.sql.functions import col, current_timestamp

raw_stream = (
    spark.readStream
        .format("cloudFiles")
        .option("cloudFiles.format",              "json")
        .option("cloudFiles.schemaLocation",      SCHEMA_LOCATION)
        .option("cloudFiles.inferColumnTypes",    "true")
        .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
        .option("cloudFiles.partitionColumns",    "marketplace,date")
        .option("rescuedDataColumn",              "_rescued_data")
        .option("multiLine",                      "false")
        .load(RAW_MKT_PATH)
)

print("Stream schema (after inference from sample files):")
raw_stream.printSchema()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Add ingestion metadata

# COMMAND ----------

enriched_stream = (
    raw_stream
        .withColumn("_ingest_ts",   current_timestamp())
        .withColumn("_source_file", col("_metadata.file_path"))
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Skipping pre-create
# MAGIC
# MAGIC Same reason as POS RTLOG: clustering needs a defined schema, which only exists after the
# MAGIC first writeStream batch. The next cell creates the table from the inferred schema.

# COMMAND ----------

print("Table will be created by writeStream in the next cell.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Write stream — `trigger(availableNow=True)`

# COMMAND ----------

stream_query = (
    enriched_stream.writeStream
        .format("delta")
        .option("checkpointLocation", CHECKPOINT_LOCATION)
        .option("mergeSchema",        "true")
        .trigger(availableNow=True)
        .toTable(TARGET_TABLE)
)

stream_query.awaitTermination()
print("Stream completed.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5b. Apply table properties (post-write)
# MAGIC
# MAGIC Auto-optimize + a descriptive comment. No clustering at Bronze.

# COMMAND ----------

spark.sql(f"""
ALTER TABLE {TARGET_TABLE} SET TBLPROPERTIES (
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.autoOptimize.autoCompact'   = 'true'
)
""")

spark.sql(f"""
COMMENT ON TABLE {TARGET_TABLE} IS
'Bronze marketplace feeds — third-party marketplace orders, rtlog_orig_sys=MKT, ingested via Auto Loader'
""")

print(f"Properties applied to {TARGET_TABLE}.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Verify — row count and partition distribution

# COMMAND ----------

display(spark.sql(f"""
SELECT
    COUNT(*) AS total_orders,
    COUNT(DISTINCT marketplace) AS distinct_marketplaces,
    COUNT(DISTINCT date)        AS distinct_dates,
    MIN(date)                   AS first_date,
    MAX(date)                   AS last_date
FROM {TARGET_TABLE}
"""))

# COMMAND ----------

display(spark.sql(f"""
SELECT
    marketplace,
    COUNT(*)             AS order_count,
    COUNT(DISTINCT date) AS dates_present,
    MIN(date)            AS first_date,
    MAX(date)            AS last_date
FROM {TARGET_TABLE}
GROUP BY marketplace
ORDER BY order_count DESC
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Show inferred schema

# COMMAND ----------

display(spark.sql(f"DESCRIBE {TARGET_TABLE}"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Inspect `_rescued_data`
# MAGIC
# MAGIC Any row whose JSON didn't match the inferred schema lands here as a JSON string.
# MAGIC For a clean stream this should be empty.

# COMMAND ----------

rescued_count = spark.sql(f"""
SELECT COUNT(*) AS rescued_rows
FROM {TARGET_TABLE}
WHERE _rescued_data IS NOT NULL
""").collect()[0]["rescued_rows"]

print(f"Rescued rows (schema mismatches): {rescued_count:,}")

if rescued_count > 0:
    print("\nSample rescued rows:")
    display(spark.sql(f"""
        SELECT _source_file, _rescued_data
        FROM {TARGET_TABLE}
        WHERE _rescued_data IS NOT NULL
        LIMIT 10
    """))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 9. Spot check — marketplace × date sample

# COMMAND ----------

# Show first record to inspect actual nested field paths
print("Sample record (first row):")
display(spark.sql(f"SELECT * FROM {TARGET_TABLE} LIMIT 1"))

# COMMAND ----------

# Verify the channel marker is set correctly (rtlog_orig_sys should always be 'MKT' here).
# Adjust the field path if the JSON uses a different casing/structure (e.g., RTLOG_ORIG_SYS).
display(spark.sql(f"""
SELECT
    rtlog_orig_sys,
    COUNT(*) AS row_count
FROM {TARGET_TABLE}
GROUP BY rtlog_orig_sys
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 10. Confirm physical location is in the bronze container

# COMMAND ----------

display(spark.sql(f"DESCRIBE EXTENDED {TARGET_TABLE}"))
# Look for the `Location` row — should start with abfss://bronze@stretaildpsatyaki01...

# COMMAND ----------

# MAGIC %md
# MAGIC ## Re-running this notebook
# MAGIC
# MAGIC Safe. Auto Loader uses the checkpoint to skip files already processed. Workflow:
# MAGIC
# MAGIC 1. Marketplace simulator produces new NDJSON files locally
# MAGIC 2. ADLS sync console uploads them to `raw/marketplace/marketplace=*/date=*/`
# MAGIC 3. Re-run this notebook → only the new files are ingested
# MAGIC
# MAGIC ## Bronze layer complete
# MAGIC
# MAGIC With this notebook done, all 5 raw sources are in `retaildp.bronze`:
# MAGIC
# MAGIC | Bronze table | Source | Pattern |
# MAGIC |---|---|---|
# MAGIC | `fx_rates` | `raw/fx-rates/*.csv` | Batch CSV → MERGE |
# MAGIC | `weather` | `raw/weather/*.csv` | Batch CSV → MERGE |
# MAGIC | `olist_*` (9 tables) | `raw/olist/*.csv` | Batch CSV → OVERWRITE |
# MAGIC | `pos_rtlog` | `raw/pos/store=*/date=*/hour=*/` | Auto Loader → append |
# MAGIC | `marketplace` | `raw/marketplace/marketplace=*/date=*/` | Auto Loader → append |
# MAGIC
# MAGIC **Next layer:** Silver — ReSA `SA_TRAN_*` canonical model. Eight tables, one notebook each,
# MAGIC reading from `pos_rtlog` and `marketplace` (both have `rtlog_orig_sys` marker) and joining
# MAGIC to `fx_rates` for USD normalization.
