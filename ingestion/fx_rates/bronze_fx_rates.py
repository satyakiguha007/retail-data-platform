# Databricks notebook source

# COMMAND ----------
# MAGIC %md
# MAGIC # Bronze: FX Rates
# MAGIC
# MAGIC Ingests the daily FX rates CSV feed from the ADLS landing zone into
# MAGIC `bronze.fx_rates` as a Delta table.
# MAGIC
# MAGIC **Source:** `{landing_root}/fx_rates/YYYY/MM/DD/fx_rates_*.csv`
# MAGIC **Target:** `bronze.fx_rates`  (partitioned by `rate_date`)
# MAGIC **Schedule:** Daily at 02:00 UTC (after upstream publishes the file)
# MAGIC
# MAGIC Idempotent: uses MERGE on `(rate_date, from_currency, to_currency)`.

# COMMAND ----------

dbutils.widgets.text("landing_root", "abfss://landing@<storage>.dfs.core.windows.net", "Landing zone root (ADLS path or local)")
dbutils.widgets.text("catalog",      "retail",   "Unity Catalog name")
dbutils.widgets.text("bronze_schema","bronze",   "Bronze schema name")
dbutils.widgets.text("run_date",     "",         "Process date YYYY-MM-DD (blank = today)")

# COMMAND ----------

from datetime import date, datetime
from pyspark.sql import functions as F
from pyspark.sql.types import (
    DateType, DecimalType, StringType, StructField, StructType, TimestampType,
)

landing_root  = dbutils.widgets.get("landing_root").rstrip("/")
catalog       = dbutils.widgets.get("catalog")
bronze_schema = dbutils.widgets.get("bronze_schema")
run_date_str  = dbutils.widgets.get("run_date").strip()

run_date = date.fromisoformat(run_date_str) if run_date_str else date.today()
ingested_at = datetime.utcnow()

target_table = f"`{catalog}`.`{bronze_schema}`.fx_rates"
source_path  = f"{landing_root}/fx_rates/{run_date.strftime('%Y/%m/%d')}/"

print(f"run_date    : {run_date}")
print(f"source_path : {source_path}")
print(f"target      : {target_table}")

# COMMAND ----------
# MAGIC %md ## 1. Create target table (idempotent DDL)

# COMMAND ----------

spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {target_table} (
        rate_date       DATE          NOT NULL,
        from_currency   STRING        NOT NULL,
        to_currency     STRING        NOT NULL,
        rate            DECIMAL(18,6) NOT NULL,
        ingested_at     TIMESTAMP     NOT NULL,
        source_file     STRING
    )
    USING DELTA
    PARTITIONED BY (rate_date)
    TBLPROPERTIES (
        'delta.enableChangeDataFeed' = 'false',
        'delta.autoOptimize.optimizeWrite' = 'true'
    )
""")

# COMMAND ----------
# MAGIC %md ## 2. Read CSV with enforced schema

# COMMAND ----------

_SCHEMA = StructType([
    StructField("rate_date",     StringType(),  nullable=False),
    StructField("from_currency", StringType(),  nullable=False),
    StructField("to_currency",   StringType(),  nullable=False),
    StructField("rate",          DecimalType(18, 6), nullable=False),
])

raw = (
    spark.read
         .option("header", "true")
         .option("mode", "FAILFAST")       # surface malformed rows immediately
         .schema(_SCHEMA)
         .csv(source_path)
         .withColumn("rate_date",    F.to_date("rate_date", "yyyy-MM-dd"))
         .withColumn("ingested_at",  F.lit(ingested_at).cast(TimestampType()))
         .withColumn("source_file",  F.input_file_name())
)

# COMMAND ----------
# MAGIC %md ## 3. Quality checks (fail-fast before writing)

# COMMAND ----------

row_count = raw.count()
assert row_count > 0, f"No rows read from {source_path}"

# All rates must be positive
bad_rates = raw.filter(F.col("rate") <= 0).count()
assert bad_rates == 0, f"{bad_rates} rows have non-positive rate"

# No future dates
future_rows = raw.filter(F.col("rate_date") > F.lit(run_date)).count()
assert future_rows == 0, f"{future_rows} rows have future rate_date"

# to_currency is always USD in this feed
non_usd = raw.filter(F.col("to_currency") != "USD").count()
assert non_usd == 0, f"{non_usd} rows have unexpected to_currency"

# Known currency set
known_ccys = {"INR", "GBP", "AED", "SGD", "USD"}
unknown_ccys = (
    raw.select("from_currency").distinct()
       .filter(~F.col("from_currency").isin(list(known_ccys)))
       .collect()
)
assert not unknown_ccys, f"Unknown from_currency values: {[r[0] for r in unknown_ccys]}"

print(f"Quality checks passed — {row_count:,} rows, {raw.select('from_currency').distinct().count()} currencies")

# COMMAND ----------
# MAGIC %md ## 4. MERGE into bronze.fx_rates

# COMMAND ----------

raw.createOrReplaceTempView("_fx_staged")

spark.sql(f"""
    MERGE INTO {target_table} AS tgt
    USING _fx_staged AS src
    ON  tgt.rate_date     = src.rate_date
    AND tgt.from_currency = src.from_currency
    AND tgt.to_currency   = src.to_currency
    WHEN MATCHED THEN UPDATE SET
        tgt.rate         = src.rate,
        tgt.ingested_at  = src.ingested_at,
        tgt.source_file  = src.source_file
    WHEN NOT MATCHED THEN INSERT *
""")

# COMMAND ----------
# MAGIC %md ## 5. Verify and report

# COMMAND ----------

summary = spark.sql(f"""
    SELECT
        from_currency,
        COUNT(*)                       AS days_loaded,
        MIN(rate_date)                 AS earliest_date,
        MAX(rate_date)                 AS latest_date,
        ROUND(MIN(CAST(rate AS DOUBLE)), 6) AS min_rate,
        ROUND(MAX(CAST(rate AS DOUBLE)), 6) AS max_rate
    FROM {target_table}
    GROUP BY from_currency
    ORDER BY from_currency
""")
display(summary)
