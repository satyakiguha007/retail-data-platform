# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze — `stores`
# MAGIC
# MAGIC Loads the store master CSV into Bronze as a static reference dimension.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source** | `abfss://raw@stretaildpsatyaki01.dfs.core.windows.net/stores/stores.csv` |
# MAGIC | **Target** | `retaildp.bronze.stores` |
# MAGIC | **Pattern** | Batch CSV + OVERWRITE (static historical, atomic replace) |
# MAGIC | **Idempotent** | Yes — overwrite produces identical state |
# MAGIC | **Schema** | Strict `StructType`, header skipped, no inference |

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql.functions import current_timestamp, lit
from pyspark.sql.types import (
    StructType, StructField, LongType, StringType, IntegerType, DecimalType,
)

# Widget lets you point at a different folder or filename (e.g. for tests / backfills)
dbutils.widgets.text(
    "source_path",
    "abfss://raw@stretaildpsatyaki01.dfs.core.windows.net/stores/stores.csv",
    "Source CSV path",
)
SOURCE_PATH = dbutils.widgets.get("source_path")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

TARGET_TABLE = "retaildp.bronze.stores"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Source schema
# MAGIC
# MAGIC Header row of `stores.csv`:
# MAGIC `store_no, store_name, tills, country, local_currency, exchange_rate_to_usd`

# COMMAND ----------

stores_schema = StructType([
    StructField("store_no",             LongType(),         nullable=False),
    StructField("store_name",           StringType(),       nullable=False),
    StructField("tills",                IntegerType(),      nullable=False),
    StructField("country",              StringType(),       nullable=False),
    StructField("local_currency",       StringType(),       nullable=False),
    StructField("exchange_rate_to_usd", DecimalType(20, 4), nullable=False),
])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Read CSV

# COMMAND ----------

raw_df = (
    spark.read
    .format("csv")
    .option("header", "true")
    .schema(stores_schema)
    .load(SOURCE_PATH)
)

# Stamp Bronze lineage (same convention as the other Bronze notebooks)
bronze_df = (
    raw_df
    .withColumn("_ingest_ts",   current_timestamp())
    .withColumn("_source_file", lit(SOURCE_PATH))
)

print(f"Read {bronze_df.count()} rows from {SOURCE_PATH}")
display(bronze_df.limit(5))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Write to Bronze (OVERWRITE)
# MAGIC
# MAGIC Static reference dimension — atomic full-overwrite on every run.
# MAGIC No partitioning (26 rows).

# COMMAND ----------

(
    bronze_df.write
    .format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .option("delta.autoOptimize.optimizeWrite", "true")
    .option("delta.autoOptimize.autoCompact",   "true")
    .saveAsTable(TARGET_TABLE)
)
print(f"Wrote {TARGET_TABLE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Validation

# COMMAND ----------

# Row count
print(f"{TARGET_TABLE} row count: {spark.table(TARGET_TABLE).count()}")

# PK uniqueness on store_no
dup_count = (
    spark.table(TARGET_TABLE)
    .groupBy("store_no").count()
    .where("count > 1").count()
)
assert dup_count == 0, f"PK violation: {dup_count} duplicate store_no values"
print("PK uniqueness check passed (0 duplicates)")

# Sample
display(spark.table(TARGET_TABLE).orderBy("store_no"))
