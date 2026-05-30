# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_store_data`
# MAGIC
# MAGIC Conforms store master attributes from `bronze.stores` into the ReSA-style dimension.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source** | `retaildp.bronze.stores` |
# MAGIC | **Target** | `retaildp.silver.sa_store_data` |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_store_data_rejects` |
# MAGIC | **Pattern** | Batch read + MERGE upsert |
# MAGIC | **Idempotent** | Yes — MERGE on natural key `STORE` |
# MAGIC | **Streaming** | No — reference dimension, ~26 rows |
# MAGIC
# MAGIC ## Source → Silver mapping
# MAGIC
# MAGIC | Bronze column | Silver column | Transform |
# MAGIC |---|---|---|
# MAGIC | `store_no` | `STORE` | cast → `BIGINT` |
# MAGIC | `store_name` | `STORE_NAME` | trim |
# MAGIC | `tills` | `REGISTER_COUNT` | cast → `INT` (ReSA uses "REGISTER", synonym for "till") |
# MAGIC | `country` | `COUNTRY` | map long-form → ISO3 (India→IND, USA→USA, UK→GBR, UAE→ARE, Singapore→SGP) |
# MAGIC | `local_currency` | `CURRENCY_CODE` | uppercase + trim |
# MAGIC | `exchange_rate_to_usd` | — | **dropped** — `bronze.fx_rates` is the source of truth for FX |
# MAGIC
# MAGIC ## DQ rules (failures route to quarantine, never silently dropped)
# MAGIC 1. `STORE` NOT NULL and > 0
# MAGIC 2. `STORE_NAME` NOT NULL and non-empty after trim
# MAGIC 3. `REGISTER_COUNT` NOT NULL and > 0
# MAGIC 4. `COUNTRY` resolved to a valid ISO3 code (unmapped country → null → reject)
# MAGIC 5. `CURRENCY_CODE` in `{INR, USD, GBP, AED, SGD}`

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, current_timestamp, lit, when, trim, upper,
    array, array_compact, length,
)
from pyspark.sql.types import (
    StructType, StructField, LongType, StringType, IntegerType,
    TimestampType, ArrayType,
)
from delta.tables import DeltaTable

# Widget allows overriding the source table for tests or backfills.
dbutils.widgets.text("source_table", "retaildp.bronze.stores", "Source Bronze Table")
SOURCE_TABLE = dbutils.widgets.get("source_table")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

TARGET_TABLE     = "retaildp.silver.sa_store_data"
QUARANTINE_TABLE = "retaildp.quarantine.silver_sa_store_data_rejects"

# DQ reference sets
VALID_CURRENCIES = {"INR", "USD", "GBP", "AED", "SGD"}
VALID_COUNTRIES  = {"IND", "USA", "GBR", "ARE", "SGP"}

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema
# MAGIC
# MAGIC Explicit `StructType` — no inference. Type mapping per `CLAUDE.md`:
# MAGIC `NUMERIC` → `BIGINT` / `INT`, `VARCHAR` → `STRING`.

# COMMAND ----------

sa_store_data_schema = StructType([
    # ReSA-faithful columns
    StructField("STORE",          LongType(),    nullable=False),  # NUMERIC(10)
    StructField("STORE_NAME",     StringType(),  nullable=False),  # VARCHAR(150)
    StructField("REGISTER_COUNT", IntegerType(), nullable=False),  # from `tills`
    StructField("COUNTRY",        StringType(),  nullable=False),  # ISO3
    StructField("CURRENCY_CODE",  StringType(),  nullable=False),  # ISO4217

    # Lineage (our convention, not ReSA)
    StructField("_silver_ts",     TimestampType(), nullable=False),
    StructField("_source",        StringType(),    nullable=False),
])

# Quarantine schema = target columns + rejection_reason + quarantine timestamp
quarantine_schema = StructType(
    sa_store_data_schema.fields + [
        StructField("rejection_reason", ArrayType(StringType()), nullable=False),
        StructField("_quarantine_ts",   TimestampType(),         nullable=False),
    ]
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Read from Bronze

# COMMAND ----------

bronze_df = spark.read.table(SOURCE_TABLE)

print(f"Read {bronze_df.count()} rows from {SOURCE_TABLE}")
display(bronze_df.limit(5))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Conformance
# MAGIC
# MAGIC Rename → cast → normalise → map country to ISO3 → drop unused → stamp lineage → project.

# COMMAND ----------

conformed_df = (
    bronze_df
    # Rename to ReSA column names.
    # NOTE: `country` is renamed to a temp `_country_src` BEFORE the COUNTRY mapping
    # below, because Spark is case-insensitive by default and `withColumn("COUNTRY", ...)`
    # would otherwise replace the lowercase `country` in place (keeping the original
    # lowercase name in the schema), which would then get dropped a few lines later.
    .withColumnRenamed("store_no",       "STORE")
    .withColumnRenamed("store_name",     "STORE_NAME")
    .withColumnRenamed("tills",          "REGISTER_COUNT")
    .withColumnRenamed("local_currency", "CURRENCY_CODE")
    .withColumnRenamed("country",        "_country_src")

    # Map country long-form → ISO3. Unknown values become NULL and will fail DQ.
    .withColumn(
        "COUNTRY",
        when(col("_country_src") == "India",     lit("IND"))
        .when(col("_country_src") == "USA",      lit("USA"))
        .when(col("_country_src") == "UK",       lit("GBR"))
        .when(col("_country_src") == "UAE",      lit("ARE"))
        .when(col("_country_src") == "Singapore", lit("SGP"))
        .otherwise(lit(None).cast(StringType())),
    )

    # Drop columns we don't carry into Silver
    .drop("_country_src", "exchange_rate_to_usd")

    # Cast to ReSA types
    .withColumn("STORE",          col("STORE").cast(LongType()))
    .withColumn("REGISTER_COUNT", col("REGISTER_COUNT").cast(IntegerType()))

    # Normalise string fields
    .withColumn("STORE_NAME",    trim(col("STORE_NAME")))
    .withColumn("CURRENCY_CODE", upper(trim(col("CURRENCY_CODE"))))

    # Lineage
    .withColumn("_silver_ts", current_timestamp())
    .withColumn("_source",    lit(SOURCE_TABLE))

    # Project to exactly the target schema column order
    .select(*[f.name for f in sa_store_data_schema.fields])
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
        when(col("STORE").isNull() | (col("STORE") <= 0),
             lit("STORE must be NOT NULL and > 0")),
        when(col("STORE_NAME").isNull() | (length(col("STORE_NAME")) == 0),
             lit("STORE_NAME must be NOT NULL and non-empty")),
        when(col("REGISTER_COUNT").isNull() | (col("REGISTER_COUNT") <= 0),
             lit("REGISTER_COUNT must be NOT NULL and > 0")),
        when(col("COUNTRY").isNull() | ~col("COUNTRY").isin(*VALID_COUNTRIES),
             lit("COUNTRY did not map to valid ISO3 set")),
        when(~col("CURRENCY_CODE").isin(*VALID_CURRENCIES),
             lit("CURRENCY_CODE not in valid ISO4217 set")),
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
# MAGIC First run: create from clean batch. Subsequent runs: MERGE on `STORE`.
# MAGIC `delta.autoOptimize` set per CLAUDE.md convention.

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
    print(f"Target {TARGET_TABLE} exists — MERGE on STORE.")
    target = DeltaTable.forName(spark, TARGET_TABLE)
    (
        target.alias("t")
        .merge(
            clean_df.alias("s"),
            "t.STORE = s.STORE",
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
# MAGIC Row count, PK-uniqueness assertion, sample display.

# COMMAND ----------

# 1. Row count — should be 26 on first clean run
silver_count = spark.table(TARGET_TABLE).count()
print(f"silver.sa_store_data row count: {silver_count}")

# 2. PK uniqueness — must be zero duplicates on STORE
dup_count = (
    spark.table(TARGET_TABLE)
    .groupBy("STORE")
    .count()
    .where("count > 1")
    .count()
)
assert dup_count == 0, f"PK violation: {dup_count} duplicate STORE values"
print("PK uniqueness check passed (0 duplicates)")

# 3. Sample
display(spark.table(TARGET_TABLE).orderBy("STORE").limit(10))
