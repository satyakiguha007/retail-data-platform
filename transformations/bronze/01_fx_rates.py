# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze: FX Rates
# MAGIC
# MAGIC **Source:** Flat CSVs at `abfss://raw@.../fx-rates/*.csv`
# MAGIC **Target:** `retaildp.bronze.fx_rates` (managed Delta in bronze container)
# MAGIC **Pattern:** Read with strict schema → quality gates → `MERGE INTO` on
# MAGIC `(rate_date, from_currency, to_currency)`
# MAGIC
# MAGIC Idempotent: safe to re-run; revised rates overwrite cleanly.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration

# COMMAND ----------

CATALOG       = "retaildp"
SCHEMA        = "bronze"
TABLE         = "fx_rates"
TARGET_TABLE  = f"{CATALOG}.{SCHEMA}.{TABLE}"

RAW_BASE      = "abfss://raw@stretaildpsatyaki01.dfs.core.windows.net/"
RAW_FX_PATH   = RAW_BASE + "fx-rates/"

spark.sql(f"USE CATALOG {CATALOG}")
print(f"Target table: {TARGET_TABLE}")
print(f"Source path:  {RAW_FX_PATH}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Strict schema (no inference)

# COMMAND ----------

from pyspark.sql.types import StructType, StructField, DateType, StringType, DoubleType

FX_SCHEMA = StructType([
    StructField("rate_date",     DateType(),   nullable=False),
    StructField("from_currency", StringType(), nullable=False),
    StructField("to_currency",   StringType(), nullable=False),
    StructField("rate",          DoubleType(), nullable=False),
])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Read raw CSVs

# COMMAND ----------

raw_df = (
    spark.read
        .option("header", "true")
        .option("dateFormat", "yyyy-MM-dd")
        .option("mode", "FAILFAST")   # any bad row aborts the read — surface issues fast
        .schema(FX_SCHEMA)
        .csv(RAW_FX_PATH + "*.csv")
)

raw_count = raw_df.count()
print(f"Read {raw_count:,} rows from {RAW_FX_PATH}")
display(raw_df.limit(5))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Quality gates (assertions — fail loudly before write)

# COMMAND ----------

from pyspark.sql.functions import col, current_date, current_timestamp, lit, input_file_name

# 4.1 No null rates
null_rates = raw_df.filter(col("rate").isNull()).count()
assert null_rates == 0, f"Quality fail: {null_rates} rows with NULL rate"

# 4.2 No non-positive rates (zero or negative is nonsense for FX)
non_positive = raw_df.filter(col("rate") <= 0).count()
assert non_positive == 0, f"Quality fail: {non_positive} rows with rate <= 0"

# 4.3 No future-dated rates
future = raw_df.filter(col("rate_date") > current_date()).count()
assert future == 0, f"Quality fail: {future} rows with future rate_date"

# 4.4 Currency codes must be 3 uppercase letters (ISO 4217 shape)
bad_curr = raw_df.filter(
    ~col("from_currency").rlike("^[A-Z]{3}$") |
    ~col("to_currency").rlike("^[A-Z]{3}$")
).count()
assert bad_curr == 0, f"Quality fail: {bad_curr} rows with malformed currency code"

print("All 4 quality gates passed.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Add ingestion metadata and stage

# COMMAND ----------

from pyspark.sql.functions import col

staged = (
    raw_df
        .withColumn("_ingest_ts",   current_timestamp())
        .withColumn("_source_file", col("_metadata.file_path"))
)

staged.createOrReplaceTempView("fx_stage")
print(f"Staged {staged.count():,} rows with metadata columns.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Create target table if it doesn't exist

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
    rate_date      DATE      NOT NULL,
    from_currency  STRING    NOT NULL,
    to_currency    STRING    NOT NULL,
    rate           DOUBLE    NOT NULL,
    _ingest_ts     TIMESTAMP,
    _source_file   STRING
)
USING DELTA
TBLPROPERTIES (
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.autoOptimize.autoCompact'   = 'true'
)
COMMENT 'Bronze FX rates — daily conversion rates from various currencies. Sourced from raw/fx-rates/.'
""")

print(f"Target table {TARGET_TABLE} ready.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. MERGE INTO (upsert)

# COMMAND ----------

merge_result = spark.sql(f"""
MERGE INTO {TARGET_TABLE} AS tgt
USING fx_stage         AS src
   ON tgt.rate_date     = src.rate_date
  AND tgt.from_currency = src.from_currency
  AND tgt.to_currency   = src.to_currency
WHEN MATCHED THEN UPDATE SET
    tgt.rate         = src.rate,
    tgt._ingest_ts   = src._ingest_ts,
    tgt._source_file = src._source_file
WHEN NOT MATCHED THEN INSERT (
    rate_date, from_currency, to_currency, rate, _ingest_ts, _source_file
) VALUES (
    src.rate_date, src.from_currency, src.to_currency, src.rate, src._ingest_ts, src._source_file
)
""")

display(merge_result)   # Shows num_affected_rows, num_updated_rows, num_inserted_rows

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Sanity summary

# COMMAND ----------

display(spark.sql(f"""
SELECT
    from_currency,
    COUNT(*)            AS row_count,
    MIN(rate_date)      AS first_date,
    MAX(rate_date)      AS last_date,
    ROUND(MIN(rate), 6) AS min_rate,
    ROUND(MAX(rate), 6) AS max_rate
FROM {TARGET_TABLE}
GROUP BY from_currency
ORDER BY from_currency
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 9. Confirm physical location is in the bronze container

# COMMAND ----------

display(spark.sql(f"DESCRIBE EXTENDED {TARGET_TABLE}"))
# Look for the `Location` row — should start with abfss://bronze@stretaildpsatyaki01...
