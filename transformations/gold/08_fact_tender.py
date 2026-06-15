# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — `08_fact_tender.py`
# MAGIC
# MAGIC Payment-side fact. One row per tender line. CDF-driven from `silver.sa_tran_tender`.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Target** | `retaildp.gold_core.fact_tender` |
# MAGIC | **Grain** | One row per `(tran_seq_no, tender_seq_no)` |
# MAGIC | **Source** | `silver.sa_tran_tender` (all 3 channels) via CDF |
# MAGIC | **Merge key** | `(tran_seq_no, tender_seq_no)` |
# MAGIC | **Clustering** | Liquid Clustering on `(business_date, store_key)` |
# MAGIC
# MAGIC ## FKs
# MAGIC `date_key`, `store_key`, `channel_key`, `tender_key` (composite group+id). Unmatched → -1.
# MAGIC
# MAGIC ## Measures
# MAGIC - `tender_amt`, `tender_amt_usd` — payment amount, local + USD
# MAGIC - `orig_curr_amt` — original-currency amount (multi-currency capture; degenerate)
# MAGIC
# MAGIC Same CDF + watermark + dim_lookup pattern as `07_fact_sales_line`.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 0. ⚠ One-time cleanup (only if rebuilding)

# COMMAND ----------

 #spark.sql("DROP TABLE IF EXISTS retaildp.gold_core.fact_tender")
 #spark.sql("DELETE FROM retaildp.gold_core._fact_load_log WHERE fact_table = 'fact_tender'")
 #print("Dropped fact_tender + cleared watermark. Next run bootstraps.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration + helpers

# COMMAND ----------

CATALOG      = "retaildp"
CORE         = "gold_core"
FACT         = "fact_tender"
FACT_FQN     = f"{CATALOG}.{CORE}.{FACT}"
SOURCE_TABLE = f"{CATALOG}.silver.sa_tran_tender"

print(f"Target: {FACT_FQN}")
print(f"Source: {SOURCE_TABLE}")

# COMMAND ----------

# MAGIC %run ../gold/_shared/watermark

# COMMAND ----------

# MAGIC %run ../gold/_shared/cdf_reader

# COMMAND ----------

# MAGIC %run ../gold/_shared/dim_lookup

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Create `fact_tender`

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {FACT_FQN} (
    date_key         BIGINT     COMMENT 'FK dim_date (-1 = unknown)',
    store_key        BIGINT     COMMENT 'FK dim_store (-1 = unknown)',
    channel_key      BIGINT     COMMENT 'FK dim_channel (-1 = unknown)',
    tender_key       BIGINT     COMMENT 'FK dim_tender (-1 = unknown)',

    tran_seq_no      BIGINT     NOT NULL  COMMENT 'Transaction surrogate (degenerate)',
    tender_seq_no    INT        NOT NULL  COMMENT 'Tender line number (degenerate)',
    business_date    DATE       NOT NULL  COMMENT 'Clustering key',
    rtlog_orig_sys   STRING               COMMENT 'Raw channel discriminator (degenerate)',
    tender_type_group STRING              COMMENT 'Raw tender group (degenerate; tender_key is FK)',
    tender_type_id   INT                  COMMENT 'Raw tender id (degenerate)',

    tender_amt       DECIMAL(20,4)  COMMENT 'Payment amount, local currency',
    tender_amt_usd   DECIMAL(20,4)  COMMENT 'Payment amount, USD',
    orig_curr_amt    DECIMAL(20,4)  COMMENT 'Amount in original tender currency (multi-currency)',
    orig_currency    STRING         COMMENT 'Original tender currency code',

    _load_ts         TIMESTAMP  NOT NULL  COMMENT 'When loaded/updated',
    _source          STRING               COMMENT 'Source table'
)
USING DELTA
CLUSTER BY (business_date, store_key)
TBLPROPERTIES (
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.autoOptimize.autoCompact'   = 'true'
)
COMMENT 'Payment fact — one row per tender line. CDF-driven from silver.sa_tran_tender. Grain (tran_seq_no, tender_seq_no).'
""")

print(f"{FACT_FQN} ready.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Read source via CDF + resolve watermark window

# COMMAND ----------

last_wm = get_watermark(FACT)
print(f"Current watermark for {FACT}: {last_wm}")

cdf_df, version_to = read_cdf_since(SOURCE_TABLE, last_wm)
version_from = last_wm + 1

src_count = cdf_df.count()
print(f"Window: v{version_from}..v{version_to}  |  source rows: {src_count:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Build fact rows — measures + dim keys

# COMMAND ----------

from pyspark.sql import functions as F

run_id = None
clean_n = 0

if src_count == 0:
    print("No new source rows — nothing to load. Watermark unchanged.")
else:
    run_id = start_fact_run(FACT, SOURCE_TABLE, version_from, version_to)

    try:
        keyed = (
            cdf_df
            .transform(add_date_key)       # BUSINESS_DATE -> date_key
            .transform(add_store_key)      # STORE -> store_key
            .transform(add_channel_key)    # RTLOG_ORIG_SYS -> channel_key
            .transform(add_tender_key)     # (TENDER_TYPE_GROUP, TENDER_TYPE_ID) -> tender_key
        )

        staged = keyed.select(
            F.col("date_key"),
            F.col("store_key"),
            F.col("channel_key"),
            F.col("tender_key"),
            F.col("TRAN_SEQ_NO").alias("tran_seq_no"),
            F.col("TENDER_SEQ_NO").alias("tender_seq_no"),
            F.col("BUSINESS_DATE").alias("business_date"),
            F.col("RTLOG_ORIG_SYS").alias("rtlog_orig_sys"),
            F.col("TENDER_TYPE_GROUP").alias("tender_type_group"),
            F.col("TENDER_TYPE_ID").alias("tender_type_id"),
            F.col("TENDER_AMT").alias("tender_amt"),
            F.col("TENDER_AMT_USD").alias("tender_amt_usd"),
            F.col("ORIG_CURR_AMT").alias("orig_curr_amt"),
            F.col("ORIG_CURRENCY").alias("orig_currency"),
            F.current_timestamp().alias("_load_ts"),
            F.lit(SOURCE_TABLE).alias("_source"),
        ).where(
            F.col("tran_seq_no").isNotNull() & F.col("tender_seq_no").isNotNull()
        )

        staged.createOrReplaceTempView("fact_tender_staged")
        clean_n = staged.count()
        print(f"Staged fact rows: {clean_n:,}")

    except Exception as e:
        fail_fact_run(run_id, str(e))
        raise

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. MERGE into the fact

# COMMAND ----------

if src_count > 0:
    try:
        before = spark.table(FACT_FQN).count()

        spark.sql(f"""
            MERGE INTO {FACT_FQN} AS tgt
            USING fact_tender_staged AS src
            ON tgt.tran_seq_no = src.tran_seq_no
           AND tgt.tender_seq_no = src.tender_seq_no
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *
        """)

        after = spark.table(FACT_FQN).count()
        rows_inserted = after - before
        rows_updated  = max(clean_n - rows_inserted, 0)

        complete_fact_run(run_id, rows_inserted=rows_inserted,
                          rows_updated=rows_updated, rows_deleted=0)
        print(f"MERGE done. inserted={rows_inserted:,} updated≈{rows_updated:,}")

    except Exception as e:
        fail_fact_run(run_id, str(e))
        raise

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Validation

# COMMAND ----------

print("=== Channel split ===")
display(spark.sql(f"""
    SELECT rtlog_orig_sys, COUNT(*) AS rows
    FROM {FACT_FQN} GROUP BY rtlog_orig_sys ORDER BY rtlog_orig_sys
"""))

# COMMAND ----------

print("=== Orphan FK check ===")
display(spark.sql(f"""
    SELECT
        SUM(CASE WHEN date_key    = -1 THEN 1 ELSE 0 END) AS orphan_date,
        SUM(CASE WHEN store_key   = -1 THEN 1 ELSE 0 END) AS orphan_store,
        SUM(CASE WHEN channel_key = -1 THEN 1 ELSE 0 END) AS orphan_channel,
        SUM(CASE WHEN tender_key  = -1 THEN 1 ELSE 0 END) AS orphan_tender
    FROM {FACT_FQN}
"""))

# COMMAND ----------

print("=== PK uniqueness ===")
display(spark.sql(f"""
    SELECT COUNT(*) AS duplicate_keys FROM (
        SELECT tran_seq_no, tender_seq_no, COUNT(*) c
        FROM {FACT_FQN} GROUP BY tran_seq_no, tender_seq_no HAVING COUNT(*) > 1
    )
"""))

# COMMAND ----------

print("=== Row reconciliation: fact == silver ===")
display(spark.sql(f"""
    SELECT
        (SELECT COUNT(*) FROM {SOURCE_TABLE}) AS silver_rows,
        (SELECT COUNT(*) FROM {FACT_FQN})     AS fact_rows
"""))

# COMMAND ----------

print("=== Payment mix by channel (USD) ===")
display(spark.sql(f"""
    SELECT
        f.rtlog_orig_sys,
        t.tender_type_group,
        COUNT(*)                       AS lines,
        ROUND(SUM(f.tender_amt_usd),2) AS amt_usd
    FROM {FACT_FQN} f
    LEFT JOIN {CATALOG}.{CORE}.dim_tender t ON f.tender_key = t.tender_key
    GROUP BY f.rtlog_orig_sys, t.tender_type_group
    ORDER BY f.rtlog_orig_sys, amt_usd DESC
"""))

# COMMAND ----------

print("=== Watermark log ===")
display(spark.sql(f"""
    SELECT run_id, version_from, version_to, rows_inserted, rows_updated, run_status
    FROM {CATALOG}.{CORE}._fact_load_log
    WHERE fact_table = '{FACT}' ORDER BY run_start_ts DESC LIMIT 5
"""))

# COMMAND ----------

print(f"✅ fact_tender load complete.")
print(f"   Fact rows:     {spark.table(FACT_FQN).count():,}")
print(f"   New watermark: v{version_to}")
