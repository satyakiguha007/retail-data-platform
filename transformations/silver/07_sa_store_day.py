# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_store_day`
# MAGIC
# MAGIC Derives the per-store-per-business-date parent table from `bronze.pos_rtlog`.
# MAGIC Every `sa_tran_*` child table will reference `STORE_DAY_SEQ_NO` from here.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source** | `retaildp.bronze.pos_rtlog` |
# MAGIC | **Target** | `retaildp.silver.sa_store_day` |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_store_day_rejects` |
# MAGIC | **FK lookup** | `retaildp.silver.sa_store_data` (broadcast join — STORE must exist) |
# MAGIC | **Pattern** | `readStream` + `availableNow` + `foreachBatch` → MERGE |
# MAGIC | **Idempotent** | Yes — checkpoint-driven incremental + idempotent MERGE on `STORE_DAY_SEQ_NO` |
# MAGIC | **Partitioned by** | `BUSINESS_DATE` |
# MAGIC
# MAGIC ## Key derivation
# MAGIC - `STORE_DAY_SEQ_NO` = `xxhash64(STORE, BUSINESS_DATE)` → `BIGINT` (deterministic surrogate)
# MAGIC - `DAY` = `dayofmonth(BUSINESS_DATE)` — ReSA `NUMERIC(3)`-fidelity column (1–31, cycles monthly). The actual unique identity comes from `STORE_DAY_SEQ_NO`.
# MAGIC - `AUDIT_STATUS` = `'A'` (Awaiting). In real ReSA this transitions A→V→P during audit; our lakehouse holds it at A as a placeholder.
# MAGIC
# MAGIC ## DQ rules (failures routed to quarantine)
# MAGIC 1. `STORE` NOT NULL and > 0
# MAGIC 2. `BUSINESS_DATE` NOT NULL
# MAGIC 3. `STORE` exists in `silver.sa_store_data` (FK violation if not)
# MAGIC
# MAGIC ## Bronze.pos_rtlog columns assumed
# MAGIC `store` (BIGINT), `business_date` (DATE), `tran_ts` (TIMESTAMP). Other columns ignored.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, current_timestamp, lit, when, broadcast,
    array, array_compact,
    count as f_count, min as f_min, max as f_max,
    least, greatest, xxhash64, dayofmonth,
)
from pyspark.sql.types import (
    StructType, StructField, LongType, IntegerType, StringType,
    DateType, TimestampType, ArrayType,
)
from delta.tables import DeltaTable

# Widget allows overriding the source for tests/backfills
dbutils.widgets.text("source_table", "retaildp.bronze.pos_rtlog", "Source Bronze Table")
SOURCE_TABLE = dbutils.widgets.get("source_table")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

TARGET_TABLE       = "retaildp.silver.sa_store_day"
QUARANTINE_TABLE   = "retaildp.quarantine.silver_sa_store_day_rejects"
STORE_MASTER_TABLE = "retaildp.silver.sa_store_data"
CHECKPOINT_PATH    = (
    "abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/silver/sa_store_day/"
)

# COMMAND ----------

# Diagnostic — share output back with Claude

print("=== sa_store_data check ===")
try:
    n = spark.table("retaildp.silver.sa_store_data").count()
    print(f"silver.sa_store_data EXISTS with {n} rows")
except Exception as e:
    print(f"silver.sa_store_data MISSING — error: {e}")

print("\n=== bronze.pos_rtlog schema ===")
spark.table("retaildp.bronze.pos_rtlog").printSchema()

print("\n=== bronze.pos_rtlog sample (1 row, vertical) ===")
spark.table("retaildp.bronze.pos_rtlog").limit(1).show(vertical=True, truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema
# MAGIC
# MAGIC Explicit `StructType`. `STORE_DAY_SEQ_NO` is the surrogate PK every
# MAGIC downstream `sa_tran_*` child joins back to.

# COMMAND ----------

sa_store_day_schema = StructType([
    # Identity
    StructField("STORE_DAY_SEQ_NO",   LongType(),      nullable=False),  # surrogate (xxhash64)
    StructField("STORE",              LongType(),      nullable=False),
    StructField("BUSINESS_DATE",      DateType(),      nullable=False),  # partition
    StructField("DAY",                IntegerType(),   nullable=False),  # ReSA NUMERIC(3) fidelity

    # Operational state
    StructField("AUDIT_STATUS",       StringType(),    nullable=False),  # 'A'/'V'/'P'

    # Activity metadata (operational rollup; updated incrementally per microbatch)
    StructField("RTLOG_RECORD_COUNT", LongType(),      nullable=False),
    StructField("FIRST_TRAN_TS",      TimestampType(), nullable=True),
    StructField("LAST_TRAN_TS",       TimestampType(), nullable=True),

    # Lineage
    StructField("_silver_ts",         TimestampType(), nullable=False),
    StructField("_source",            StringType(),    nullable=False),
])

# Quarantine schema = target columns + rejection_reason + quarantine timestamp
quarantine_schema = StructType(
    sa_store_day_schema.fields + [
        StructField("rejection_reason", ArrayType(StringType()), nullable=False),
        StructField("_quarantine_ts",   TimestampType(),         nullable=False),
    ]
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bootstrap target table if absent
# MAGIC
# MAGIC Streaming MERGE requires the target Delta table to exist. Create an empty
# MAGIC table with the right schema and partitioning the first time this runs.

# COMMAND ----------

if not spark.catalog.tableExists(TARGET_TABLE):
    print(f"Creating empty {TARGET_TABLE} partitioned by BUSINESS_DATE.")
    (
        spark.createDataFrame([], sa_store_day_schema).write
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
# MAGIC ## foreachBatch handler
# MAGIC
# MAGIC Inside each micro-batch:
# MAGIC 1. Aggregate the batch to `(store, business_date)` with record count and first/last timestamps.
# MAGIC 2. Conform — rename to ReSA columns, derive `STORE_DAY_SEQ_NO`, `DAY`, `AUDIT_STATUS`.
# MAGIC 3. FK-validate `STORE` against `silver.sa_store_data` (broadcast join — only 26 rows).
# MAGIC 4. MERGE clean rows into target:
# MAGIC    - **Matched** → incremental: `RTLOG_RECORD_COUNT += new`, `FIRST_TRAN_TS = least(old, new)`, `LAST_TRAN_TS = greatest(old, new)`.
# MAGIC    - **Not matched** → insert full row.
# MAGIC 5. Append rejects (if any) to quarantine.

# COMMAND ----------

def merge_microbatch(microBatchDF: DataFrame, batch_id: int) -> None:
    # 1. Aggregate this micro-batch
    agg = (
        microBatchDF
        .filter(col("store").isNotNull() & col("date").isNotNull())
        .withColumn("_tran_ts", col("tran_head.tran_datetime").cast("timestamp"))
        .groupBy("store", "date")
        .agg(
            f_count("*").alias("_record_count"),
            f_min("_tran_ts").alias("_first_ts"),
            f_max("_tran_ts").alias("_last_ts"),
        )
    )

    # 2. Conformance — rename + derive keys + lineage
    conformed = (
        agg
        .select(
            col("store").cast(LongType()).alias("STORE"),
            col("date").cast(DateType()).alias("BUSINESS_DATE"),
            col("_record_count"),
            col("_first_ts"),
            col("_last_ts"),
        )
        .withColumn("STORE_DAY_SEQ_NO", xxhash64(col("STORE"), col("BUSINESS_DATE")))
        .withColumn("DAY",              dayofmonth(col("BUSINESS_DATE")))
        .withColumn("AUDIT_STATUS",     lit("A"))
        .withColumn("_silver_ts",       current_timestamp())
        .withColumn("_source",          lit(SOURCE_TABLE))
    )

    # 3. FK validation — STORE must exist in silver.sa_store_data
    valid_stores = (
        spark.table(STORE_MASTER_TABLE)
        .select(col("STORE").alias("_valid_STORE"))
    )

    dq = (
        conformed
        .join(broadcast(valid_stores), col("STORE") == col("_valid_STORE"), "left")
        .withColumn(
            "rejection_reason",
            array_compact(array(
                when(col("STORE").isNull() | (col("STORE") <= 0),
                     lit("STORE must be NOT NULL and > 0")),
                when(col("BUSINESS_DATE").isNull(),
                     lit("BUSINESS_DATE must be NOT NULL")),
                when(col("_valid_STORE").isNull(),
                     lit("STORE not found in silver.sa_store_data (FK violation)")),
            )),
        )
        .drop("_valid_STORE")
    )

    clean   = dq.filter("size(rejection_reason) = 0").drop("rejection_reason")
    rejects = dq.filter("size(rejection_reason) > 0").withColumn(
        "_quarantine_ts", current_timestamp()
    )

    # 4. MERGE clean into target
    target = DeltaTable.forName(spark, TARGET_TABLE)
    (
        target.alias("t")
        .merge(clean.alias("s"), "t.STORE_DAY_SEQ_NO = s.STORE_DAY_SEQ_NO")
        .whenMatchedUpdate(set={
            "RTLOG_RECORD_COUNT": col("t.RTLOG_RECORD_COUNT") + col("s._record_count"),
            "FIRST_TRAN_TS":      least(col("t.FIRST_TRAN_TS"),    col("s._first_ts")),
            "LAST_TRAN_TS":       greatest(col("t.LAST_TRAN_TS"),  col("s._last_ts")),
            "_silver_ts":         col("s._silver_ts"),
        })
        .whenNotMatchedInsert(values={
            "STORE_DAY_SEQ_NO":   col("s.STORE_DAY_SEQ_NO"),
            "STORE":              col("s.STORE"),
            "BUSINESS_DATE":      col("s.BUSINESS_DATE"),
            "DAY":                col("s.DAY"),
            "AUDIT_STATUS":       col("s.AUDIT_STATUS"),
            "RTLOG_RECORD_COUNT": col("s._record_count"),
            "FIRST_TRAN_TS":      col("s._first_ts"),
            "LAST_TRAN_TS":       col("s._last_ts"),
            "_silver_ts":         col("s._silver_ts"),
            "_source":            col("s._source"),
        })
        .execute()
    )

    # 5. Quarantine rejects
    reject_n = rejects.count()
    if reject_n > 0:
        if not spark.catalog.tableExists(QUARANTINE_TABLE):
            print(f"Creating {QUARANTINE_TABLE}.")
            rejects.write.format("delta").saveAsTable(QUARANTINE_TABLE)
        else:
            rejects.write.format("delta").mode("append").saveAsTable(QUARANTINE_TABLE)

    print(f"Batch {batch_id}: clean={clean.count()} rejects={reject_n}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run the streaming merge
# MAGIC
# MAGIC `availableNow=True` — processes everything currently in the source, then stops.
# MAGIC On the next scheduled run, the checkpoint resumes from where this run ended.

# COMMAND ----------

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
# MAGIC ## Validation

# COMMAND ----------

# 1. Row count
silver_count = spark.table(TARGET_TABLE).count()
print(f"silver.sa_store_day row count: {silver_count}")

# 2. PK uniqueness on STORE_DAY_SEQ_NO
dup_count = (
    spark.table(TARGET_TABLE)
    .groupBy("STORE_DAY_SEQ_NO")
    .count()
    .where("count > 1")
    .count()
)
assert dup_count == 0, f"PK violation: {dup_count} duplicate STORE_DAY_SEQ_NO values"
print("PK uniqueness check passed (0 duplicates)")

# 3. AUDIT_STATUS domain check
bad_status = (
    spark.table(TARGET_TABLE)
    .where(~col("AUDIT_STATUS").isin("A", "V", "P"))
    .count()
)
assert bad_status == 0, f"{bad_status} rows with invalid AUDIT_STATUS"
print("AUDIT_STATUS domain check passed")

# 4. Sample — sorted by store then date
display(
    spark.table(TARGET_TABLE)
    .orderBy("STORE", "BUSINESS_DATE")
    .limit(20)
)

# COMMAND ----------

print("=== Distinct stores in bronze.pos_rtlog ===")
spark.table("retaildp.bronze.pos_rtlog").select("store").distinct().orderBy("store").show(30)

print("=== Stores in silver.sa_store_data ===")
spark.table("retaildp.silver.sa_store_data").select("STORE").orderBy("STORE").show(30)

print("=== Quarantine count ===")
try:
    n = spark.table("retaildp.quarantine.silver_sa_store_day_rejects").count()
    print(f"sa_store_day_rejects: {n} rows")
    spark.table("retaildp.quarantine.silver_sa_store_day_rejects") \
        .select("STORE", "BUSINESS_DATE", "rejection_reason").show(10, truncate=False)
except Exception as e:
    print(f"No quarantine table: {e}")
