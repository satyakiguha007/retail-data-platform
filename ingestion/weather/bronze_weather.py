# Databricks notebook source

# COMMAND ----------
# MAGIC %md
# MAGIC # Bronze: Daily Weather Data
# MAGIC
# MAGIC Ingests daily weather observations per store city from the ADLS landing zone
# MAGIC into `bronze.weather`.
# MAGIC
# MAGIC **Source:** `{landing_root}/weather/YYYY/MM/DD/weather_*.csv`
# MAGIC **Target:** `bronze.weather` (partitioned by `obs_date`)
# MAGIC **Schedule:** Daily at 06:00 UTC
# MAGIC **Upstream:** `generate_weather.py` (synthetic) or a live weather API export
# MAGIC
# MAGIC Idempotent: MERGE on `(obs_date, store_no)`.

# COMMAND ----------

dbutils.widgets.text("landing_root", "abfss://landing@<storage>.dfs.core.windows.net", "Landing zone root")
dbutils.widgets.text("catalog",      "retail",  "Unity Catalog name")
dbutils.widgets.text("bronze_schema","bronze",  "Bronze schema name")
dbutils.widgets.text("run_date",     "",        "Process date YYYY-MM-DD (blank = today)")

# COMMAND ----------

from datetime import date, datetime
from pyspark.sql import functions as F
from pyspark.sql.types import (
    DateType, DoubleType, IntegerType, StringType,
    StructField, StructType, TimestampType,
)

landing_root  = dbutils.widgets.get("landing_root").rstrip("/")
catalog       = dbutils.widgets.get("catalog")
bronze_schema = dbutils.widgets.get("bronze_schema")
run_date_str  = dbutils.widgets.get("run_date").strip()

run_date    = date.fromisoformat(run_date_str) if run_date_str else date.today()
ingested_at = datetime.utcnow()

target_table = f"`{catalog}`.`{bronze_schema}`.weather"
source_path  = f"{landing_root}/weather/{run_date.strftime('%Y/%m/%d')}/"

print(f"run_date    : {run_date}")
print(f"source_path : {source_path}")
print(f"target      : {target_table}")

# COMMAND ----------
# MAGIC %md ## 1. Create target table (idempotent DDL)

# COMMAND ----------

spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {target_table} (
        obs_date            DATE    NOT NULL,
        store_no            INT     NOT NULL,
        city                STRING,
        country             STRING,
        temp_max_c          DOUBLE,
        temp_min_c          DOUBLE,
        precipitation_mm    DOUBLE,
        condition           STRING,
        ingested_at         TIMESTAMP NOT NULL,
        source_file         STRING
    )
    USING DELTA
    PARTITIONED BY (obs_date)
    TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true')
""")

# COMMAND ----------
# MAGIC %md ## 2. Read CSV with enforced schema

# COMMAND ----------

_SCHEMA = StructType([
    StructField("obs_date",          StringType(),  False),
    StructField("store_no",          IntegerType(), False),
    StructField("city",              StringType(),  True),
    StructField("country",           StringType(),  True),
    StructField("temp_max_c",        DoubleType(),  True),
    StructField("temp_min_c",        DoubleType(),  True),
    StructField("precipitation_mm",  DoubleType(),  True),
    StructField("condition",         StringType(),  True),
])

_VALID_CONDITIONS = ["SUNNY", "PARTLY_CLOUDY", "CLOUDY", "RAINY", "STORMY", "SNOWY"]

raw = (
    spark.read
         .option("header", "true")
         .option("mode", "FAILFAST")
         .schema(_SCHEMA)
         .csv(source_path)
         .withColumn("obs_date",     F.to_date("obs_date", "yyyy-MM-dd"))
         .withColumn("ingested_at",  F.lit(ingested_at).cast(TimestampType()))
         .withColumn("source_file",  F.input_file_name())
)

# COMMAND ----------
# MAGIC %md ## 3. Quality checks

# COMMAND ----------

row_count = raw.count()
assert row_count > 0, f"No rows read from {source_path}"

# Temperature sanity bounds
bad_temp = raw.filter(
    (F.col("temp_max_c") < -60) | (F.col("temp_max_c") > 60) |
    (F.col("temp_min_c") > F.col("temp_max_c"))
).count()
assert bad_temp == 0, f"{bad_temp} rows have invalid temperatures"

# Precipitation non-negative
bad_precip = raw.filter(F.col("precipitation_mm") < 0).count()
assert bad_precip == 0, f"{bad_precip} rows have negative precipitation"

# Valid condition codes
invalid_cond = raw.filter(~F.col("condition").isin(_VALID_CONDITIONS)).count()
assert invalid_cond == 0, f"{invalid_cond} rows have unrecognised condition code"

print(f"Quality checks passed — {row_count:,} rows, {raw.select('store_no').distinct().count()} stores")

# COMMAND ----------
# MAGIC %md ## 4. MERGE into bronze.weather

# COMMAND ----------

raw.createOrReplaceTempView("_weather_staged")

spark.sql(f"""
    MERGE INTO {target_table} AS tgt
    USING _weather_staged AS src
    ON tgt.obs_date = src.obs_date AND tgt.store_no = src.store_no
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")

# COMMAND ----------
# MAGIC %md ## 5. Verify

# COMMAND ----------

display(spark.sql(f"""
    SELECT
        country,
        COUNT(DISTINCT store_no) AS stores,
        COUNT(*)                 AS obs_rows,
        MIN(obs_date)            AS earliest,
        MAX(obs_date)            AS latest,
        ROUND(AVG(temp_max_c), 1) AS avg_max_temp_c,
        ROUND(AVG(precipitation_mm), 1) AS avg_precip_mm
    FROM {target_table}
    GROUP BY country
    ORDER BY country
"""))
