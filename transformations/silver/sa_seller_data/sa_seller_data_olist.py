# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_seller_data`
# MAGIC
# MAGIC Conforms Olist seller master into a peer dimension to `sa_store_data`.
# MAGIC
# MAGIC All Olist orders land in `sa_store_day` / `sa_tran_*` under the single virtual store
# MAGIC `STORE = 99999 (OLIST_BR)`. The store dimension therefore can't carry the
# MAGIC marketplace-seller granularity. `sa_seller_data` is the new dimension that does —
# MAGIC it's the seller-side peer to the store-side `sa_store_data`.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source** | `retaildp.bronze.olist_sellers` |
# MAGIC | **Target** | `retaildp.silver.sa_seller_data` |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_seller_data_rejects` |
# MAGIC | **Pattern** | Batch read + MERGE upsert |
# MAGIC | **Idempotent** | Yes — MERGE on natural key `SELLER_ID` |
# MAGIC | **Streaming** | No — Olist is static (Kaggle dataset, ~3,095 sellers) |
# MAGIC
# MAGIC ## Source → Silver mapping
# MAGIC
# MAGIC | Bronze column | Silver column | Transform |
# MAGIC |---|---|---|
# MAGIC | `seller_id`              | `SELLER_ID`              | trim (32-char hex hash; the natural PK in Olist) |
# MAGIC | `seller_city`            | `SELLER_CITY`            | trim (preserve Olist lowercase convention) |
# MAGIC | `seller_state`           | `SELLER_STATE`           | trim + upper (2-char Brazilian UF, e.g. SP, RJ) |
# MAGIC | `seller_zip_code_prefix` | `SELLER_ZIP_CODE_PREFIX` | cast → `INT` (5-digit prefix) |
# MAGIC
# MAGIC ## DQ rules (failures route to quarantine, never silently dropped)
# MAGIC 1. `SELLER_ID` NOT NULL and non-empty after trim (PK)
# MAGIC 2. `SELLER_STATE` if populated must be exactly 2 characters (Brazilian UF code)
# MAGIC
# MAGIC ## Downstream consumers (Pass-3 Olist)
# MAGIC - `sa_tran_head_olist.py` — `VENDOR_NO = seller_id` of order_item_id=1 (primary-seller heuristic)
# MAGIC - `sa_tran_item_olist.py` — `REF_NO5 = seller_id` per line (preserves multi-seller order granularity)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, current_timestamp, lit, when, trim, upper, length,
    array, array_compact,
)
from pyspark.sql.types import (
    StructType, StructField, StringType, IntegerType,
    TimestampType, ArrayType,
)
from delta.tables import DeltaTable

# Widget allows overriding the source table for tests or backfills.
dbutils.widgets.text("source_table", "retaildp.bronze.olist_sellers", "Source Bronze Table")
SOURCE_TABLE = dbutils.widgets.get("source_table")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

TARGET_TABLE     = "retaildp.silver.sa_seller_data"
QUARANTINE_TABLE = "retaildp.quarantine.silver_sa_seller_data_rejects"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema
# MAGIC
# MAGIC Explicit `StructType` — no inference. Mirrors the `sa_store_data` pattern:
# MAGIC identity + descriptive attributes + lineage. No surrogate key — `SELLER_ID`
# MAGIC is already a 32-char hash from Olist, so it serves as the natural PK directly.

# COMMAND ----------

sa_seller_data_schema = StructType([
    # Identity (natural PK)
    StructField("SELLER_ID",              StringType(),    nullable=False),

    # Descriptive attributes
    StructField("SELLER_CITY",            StringType(),    nullable=True),
    StructField("SELLER_STATE",           StringType(),    nullable=True),
    StructField("SELLER_ZIP_CODE_PREFIX", IntegerType(),   nullable=True),

    # Lineage
    StructField("_silver_ts",             TimestampType(), nullable=False),
    StructField("_source",                StringType(),    nullable=False),
])

# Quarantine = target columns + rejection_reason + quarantine_ts
quarantine_schema = StructType(
    sa_seller_data_schema.fields + [
        StructField("rejection_reason", ArrayType(StringType()), nullable=False),
        StructField("_quarantine_ts",   TimestampType(),         nullable=False),
    ]
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Read bronze + conform

# COMMAND ----------

bronze_df = spark.table(SOURCE_TABLE)
print(f"Read {bronze_df.count()} rows from {SOURCE_TABLE}")
display(bronze_df.limit(5))

# COMMAND ----------

conformed_df = (
    bronze_df

    # Rename to ReSA-style upper-case
    .withColumnRenamed("seller_id",              "SELLER_ID")
    .withColumnRenamed("seller_city",            "SELLER_CITY")
    .withColumnRenamed("seller_state",           "SELLER_STATE")
    .withColumnRenamed("seller_zip_code_prefix", "SELLER_ZIP_CODE_PREFIX")

    # Normalise string fields
    .withColumn("SELLER_ID",    trim(col("SELLER_ID")))
    .withColumn("SELLER_CITY",  trim(col("SELLER_CITY")))          # keep lowercase
    .withColumn("SELLER_STATE", upper(trim(col("SELLER_STATE"))))  # uppercase UF

    # Type discipline
    .withColumn("SELLER_ZIP_CODE_PREFIX", col("SELLER_ZIP_CODE_PREFIX").cast(IntegerType()))

    # Lineage
    .withColumn("_silver_ts", current_timestamp())
    .withColumn("_source",    lit(SOURCE_TABLE))

    # Project to exactly the target schema column order
    .select(*[f.name for f in sa_seller_data_schema.fields])
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## DQ split — clean vs reject
# MAGIC
# MAGIC Build a `rejection_reason` array of failed-check messages per row, then strip
# MAGIC nulls with `array_compact`. A row is **clean** iff its array is empty.
# MAGIC Rejects are never silently dropped (CLAUDE.md convention).

# COMMAND ----------

dq_df = conformed_df.withColumn(
    "rejection_reason",
    array_compact(array(
        when(col("SELLER_ID").isNull() | (length(col("SELLER_ID")) == 0),
             lit("SELLER_ID must be NOT NULL and non-empty (PK)")),
        when(col("SELLER_STATE").isNotNull() & (length(col("SELLER_STATE")) != 2),
             lit("SELLER_STATE must be exactly 2 chars (Brazilian UF code) when populated")),
    )),
)

clean_df = (
    dq_df
    .where("size(rejection_reason) = 0")
    .drop("rejection_reason")
)

reject_df = (
    dq_df
    .where("size(rejection_reason) > 0")
    .withColumn("_quarantine_ts", current_timestamp())
)

clean_n, reject_n = clean_df.count(), reject_df.count()
print(f"Clean: {clean_n} rows | Rejects: {reject_n} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## MERGE upsert into target
# MAGIC
# MAGIC First run: create from clean batch. Subsequent runs: MERGE on `SELLER_ID`.
# MAGIC `delta.autoOptimize` set per CLAUDE.md convention. No partitioning —
# MAGIC reference dimension at ~3k rows.

# COMMAND ----------

if not spark.catalog.tableExists(TARGET_TABLE):
    print(f"Target {TARGET_TABLE} does not exist — creating from clean batch.")
    (
        clean_df.write
        .format("delta")
        .option("delta.autoOptimize.optimizeWrite", "true")
        .option("delta.autoOptimize.autoCompact",   "true")
        .saveAsTable(TARGET_TABLE)
    )
else:
    print(f"Target {TARGET_TABLE} exists — MERGE on SELLER_ID.")
    target = DeltaTable.forName(spark, TARGET_TABLE)
    (
        target.alias("t")
        .merge(
            clean_df.alias("s"),
            "t.SELLER_ID = s.SELLER_ID",
        )
        .whenMatchedUpdateAll()
        .whenNotMatchedInsertAll()
        .execute()
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quarantine rejects

# COMMAND ----------

if reject_n > 0:
    if not spark.catalog.tableExists(QUARANTINE_TABLE):
        print(f"Quarantine {QUARANTINE_TABLE} does not exist — creating.")
        (
            reject_df.write
            .format("delta")
            .saveAsTable(QUARANTINE_TABLE)
        )
    else:
        print(f"Quarantine {QUARANTINE_TABLE} exists — appending.")
        (
            reject_df.write
            .format("delta")
            .mode("append")
            .saveAsTable(QUARANTINE_TABLE)
        )
else:
    print("No rejects this run.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Validation
# MAGIC
# MAGIC Row count, PK-uniqueness assertion, UF distribution, sample display.

# COMMAND ----------

# 1. Row count — should be ~3,095 on first clean run (full Olist sellers dataset)
silver_count = spark.table(TARGET_TABLE).count()
print(f"silver.sa_seller_data row count: {silver_count}")

# 2. PK uniqueness — must be zero duplicates on SELLER_ID
dup_count = (
    spark.table(TARGET_TABLE)
    .groupBy("SELLER_ID")
    .count()
    .where("count > 1")
    .count()
)
assert dup_count == 0, f"PK violation: {dup_count} duplicate SELLER_ID values"
print("PK uniqueness check passed (0 duplicates)")

# 3. UF distribution sample — sanity check that state codes look Brazilian
print("\nTop 10 SELLER_STATE distribution:")
display(
    spark.table(TARGET_TABLE)
    .groupBy("SELLER_STATE")
    .count()
    .orderBy(col("count").desc())
    .limit(10)
)

# 4. Sample rows
print("\nSample rows:")
display(spark.table(TARGET_TABLE).orderBy("SELLER_ID").limit(10))
