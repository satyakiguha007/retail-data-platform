# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze: POS RTLOG (Auto Loader)
# MAGIC
# MAGIC **Source:** NDJSON at `abfss://raw@.../pos/store=*/date=*/hour=*/rtlog.ndjson`
# MAGIC **Target:** `retaildp.bronze.pos_rtlog` — managed Delta in bronze container
# MAGIC **Pattern:** Structured Streaming + Auto Loader (`cloudFiles`) + `trigger(availableNow=True)`
# MAGIC
# MAGIC This is the first **incremental** ingestion. Auto Loader tracks which files it has already
# MAGIC processed using a checkpoint, so re-runs only pick up new files. Replays are safe.
# MAGIC
# MAGIC ## Key differences vs FX/Weather/Olist
# MAGIC
# MAGIC | Aspect | Batch (FX/Weather/Olist) | Streaming (POS RTLOG) |
# MAGIC |---|---|---|
# MAGIC | API | `spark.read.csv(...)` | `spark.readStream.format("cloudFiles").load(...)` |
# MAGIC | File tracking | Re-scans all files every run | Checkpoint remembers what's processed |
# MAGIC | Schema | Strict StructType | Auto-inferred + evolution + `_rescued_data` |
# MAGIC | Write | `MERGE` or `overwrite` | `writeStream.toTable(...)` with `trigger(availableNow=True)` |
# MAGIC | Layout | Flat | Hive-partitioned (`store=`, `date=`, `hour=`) — auto-discovered |
# MAGIC
# MAGIC ## Design philosophy
# MAGIC
# MAGIC Bronze preserves the source shape. Items, taxes, tenders, discounts stay as `ARRAY<STRUCT>`
# MAGIC columns — Silver will explode them into the ReSA `SA_TRAN_*` canonical model.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration

# COMMAND ----------

CATALOG = "retaildp"
SCHEMA  = "bronze"
TABLE   = "pos_rtlog"
TARGET_TABLE = f"{CATALOG}.{SCHEMA}.{TABLE}"

# Source — POS RTLOGs landed by the simulator + sync console
RAW_BASE     = "abfss://raw@stretaildpsatyaki01.dfs.core.windows.net/"
RAW_POS_PATH = RAW_BASE + "pos/"

# Checkpoints — Auto Loader file-tracking state + inferred schema
CHECKPOINTS_BASE  = "abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/"
SCHEMA_LOCATION   = CHECKPOINTS_BASE + "pos_rtlog/schema/"
CHECKPOINT_LOCATION = CHECKPOINTS_BASE + "pos_rtlog/state/"

spark.sql(f"USE CATALOG {CATALOG}")
print(f"Target table:        {TARGET_TABLE}")
print(f"Source path:         {RAW_POS_PATH}")
print(f"Schema location:     {SCHEMA_LOCATION}")
print(f"Checkpoint location: {CHECKPOINT_LOCATION}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Auto Loader read stream
# MAGIC
# MAGIC ### Options explained
# MAGIC
# MAGIC | Option | Value | Why |
# MAGIC |---|---|---|
# MAGIC | `cloudFiles.format` | `json` | NDJSON files |
# MAGIC | `cloudFiles.schemaLocation` | `ext_checkpoints/pos_rtlog/schema/` | Where Auto Loader persists the inferred schema between runs |
# MAGIC | `cloudFiles.inferColumnTypes` | `true` | Otherwise everything is string |
# MAGIC | `cloudFiles.schemaEvolutionMode` | `addNewColumns` | New fields trigger a restart with extended schema rather than data loss |
# MAGIC | `cloudFiles.partitionColumns` | `store,date,hour` | Tells Auto Loader to extract these from the Hive paths |
# MAGIC | `rescuedDataColumn` | `_rescued_data` | Rows that don't match the inferred schema land here as JSON, not dropped |
# MAGIC | `multiLine` | `false` | NDJSON = one JSON object per line |

# COMMAND ----------

from pyspark.sql.functions import col, current_timestamp

raw_stream = (
    spark.readStream
        .format("cloudFiles")
        .option("cloudFiles.format",              "json")
        .option("cloudFiles.schemaLocation",      SCHEMA_LOCATION)
        .option("cloudFiles.inferColumnTypes",    "true")
        .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
        .option("cloudFiles.partitionColumns",    "store,date,hour")
        .option("rescuedDataColumn",              "_rescued_data")
        .option("multiLine",                      "false")
        .load(RAW_POS_PATH)
)

print("Stream schema (after inference from sample files):")
raw_stream.printSchema()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Add ingestion metadata
# MAGIC
# MAGIC Same pattern as previous Bronze notebooks: `_ingest_ts` + `_source_file` for lineage.

# COMMAND ----------

enriched_stream = (
    raw_stream
        .withColumn("_ingest_ts",   current_timestamp())
        .withColumn("_source_file", col("_metadata.file_path"))
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Create the target table
# MAGIC
# MAGIC We let Auto Loader create the table on first write (it will use the inferred + evolved schema).
# MAGIC We just set table properties before the first write so they apply from creation.
# MAGIC
# MAGIC ### Liquid clustering vs Hive partitioning
# MAGIC
# MAGIC RTLOG events at scale = 26 stores × 365 days × ~24 hours = ~228k folders. Hive partitioning
# MAGIC on `store/date/hour` would create that many physical partitions — performance killer.
# MAGIC
# MAGIC **Liquid clustering** (`CLUSTER BY`) is the modern Delta approach: physical layout adapts to
# MAGIC query patterns automatically. Smaller files, no over-partitioning, faster point lookups.

# COMMAND ----------

# Pre-create the table with clustering + auto-optimize so the first writeStream inherits them.
# If the table doesn't exist yet, this creates an empty Delta table that Auto Loader can write to.

# Skipping pre-create.
# CLUSTER BY requires a defined schema, but Auto Loader hasn't inferred it yet
# (no rows have been read). The writeStream in the next cell will create the
# table from the inferred schema. We then apply clustering + properties below
# the writeStream once the table exists.
print("Table will be created by writeStream in the next cell.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Write stream — `trigger(availableNow=True)`
# MAGIC
# MAGIC `availableNow=True` means: process every file currently in the source, then stop. It's
# MAGIC streaming semantics (checkpointing, exactly-once delivery, partition discovery) without
# MAGIC needing a long-running cluster. Re-running picks up only new files since last run.

# COMMAND ----------

stream_query = (
    enriched_stream.writeStream
        .format("delta")
        .option("checkpointLocation", CHECKPOINT_LOCATION)
        .option("mergeSchema",        "true")
        .trigger(availableNow=True)
        .toTable(TARGET_TABLE)
)

# Wait for the stream to complete the available batch
stream_query.awaitTermination()
print("Stream completed.")

# COMMAND ----------

# Note: Liquid clustering (CLUSTER BY) requires clustering columns to have stats,
# which means they must fall within delta.dataSkippingNumIndexedCols (default 32).
# For Bronze POS RTLOG with deeply nested STRUCT columns, the partition columns
# (date, store) sit outside the stats range — so liquid clustering fails.
#
# For Bronze at this scale (~100k–500k rows), no clustering is needed. Delta's
# default file layout handles it. Layout decisions belong in Silver where we
# control the schema explicitly.

spark.sql(f"""
ALTER TABLE {TARGET_TABLE} SET TBLPROPERTIES (
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.autoOptimize.autoCompact'   = 'true'
)
""")

spark.sql(f"""
COMMENT ON TABLE {TARGET_TABLE} IS
'Bronze POS RTLOG — raw NDJSON ingested via Auto Loader, nested structure preserved (no clustering at Bronze)'
""")

print(f"Properties applied to {TARGET_TABLE}.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Verify — row count and partition distribution

# COMMAND ----------

display(spark.sql(f"""
SELECT
    COUNT(*) AS total_rtlog_events,
    COUNT(DISTINCT store) AS distinct_stores,
    COUNT(DISTINCT date)  AS distinct_dates,
    MIN(date) AS first_date,
    MAX(date) AS last_date
FROM {TARGET_TABLE}
"""))

# COMMAND ----------

display(spark.sql(f"""
SELECT
    store,
    COUNT(*) AS event_count,
    COUNT(DISTINCT date) AS dates_present
FROM {TARGET_TABLE}
GROUP BY store
ORDER BY store
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Show inferred schema (post-write)
# MAGIC
# MAGIC This is what Auto Loader figured out from the NDJSON. Should show top-level fields plus
# MAGIC nested ARRAY<STRUCT> for items/taxes/tenders/discounts.

# COMMAND ----------

display(spark.sql(f"DESCRIBE {TARGET_TABLE}"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Inspect `_rescued_data`
# MAGIC
# MAGIC Any row whose JSON didn't match the inferred schema lands here as a JSON string. For a
# MAGIC clean stream this should be empty. If rows appear here, it's a signal of schema drift in
# MAGIC the source — investigate before promoting to Silver.

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
# MAGIC ## 9. Spot check — TRAN_TYPE distribution
# MAGIC
# MAGIC Should show roughly:
# MAGIC - `SALE`         ≈ 85%
# MAGIC - `RETURN`       ≈ 5-10%
# MAGIC - `VOID`         ≈ 1-3%
# MAGIC - other types    ≈ remainder
# MAGIC
# MAGIC The exact `tran_type` field path depends on how the simulator nested it (likely
# MAGIC `tran_head.tran_type`). Adjust the query if needed once you see the schema.

# COMMAND ----------

# Inspect first record to find the right field path
print("Sample record (first row):")
display(spark.sql(f"SELECT * FROM {TARGET_TABLE} LIMIT 1"))

# COMMAND ----------

# Once you confirm the path from the sample above, uncomment and run:
# display(spark.sql(f"""
# SELECT
#     tran_head.tran_type AS tran_type,
#     COUNT(*) AS event_count,
#     ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct
# FROM {TARGET_TABLE}
# GROUP BY tran_head.tran_type
# ORDER BY event_count DESC
# """))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 10. Confirm physical location is in the bronze container

# COMMAND ----------

display(spark.sql(f"DESCRIBE EXTENDED {TARGET_TABLE}"))
# Look for the `Location` row — should start with abfss://bronze@stretaildpsatyaki01...

# COMMAND ----------

# MAGIC %md
# MAGIC ## 11. (Optional) Show checkpoint progress
# MAGIC
# MAGIC Useful when debugging incremental runs. Lists files Auto Loader has tracked.

# COMMAND ----------

# Uncomment to see what's in the checkpoint (debugging aid):
display(dbutils.fs.ls(CHECKPOINT_LOCATION))
display(dbutils.fs.ls(SCHEMA_LOCATION))

# COMMAND ----------

print(CHECKPOINT_LOCATION)
# also print the source path + schema location if they exist as variables
try:
    print(SOURCE_PATH)
except: pass
try:
    print(SCHEMA_LOCATION)
except: pass

# COMMAND ----------

# MAGIC %md
# MAGIC ## Re-running this notebook
# MAGIC
# MAGIC Safe. Auto Loader uses the checkpoint to skip files already processed. Workflow:
# MAGIC
# MAGIC 1. Simulator produces new RTLOG files locally
# MAGIC 2. ADLS sync console uploads them to `raw/pos/store=*/date=*/hour=*/`
# MAGIC 3. Re-run this notebook → only the new files are ingested
# MAGIC 4. Row count grows, but no duplicates
# MAGIC
# MAGIC ## When to wipe and restart
# MAGIC
# MAGIC If you ever need a clean state (e.g., schema drift broke things), delete both:
# MAGIC - The checkpoint folder: `abfss://checkpoints@.../pos_rtlog/`
# MAGIC - The target table: `DROP TABLE retaildp.bronze.pos_rtlog`
# MAGIC
# MAGIC Then re-run. Auto Loader will re-process every file from scratch.
