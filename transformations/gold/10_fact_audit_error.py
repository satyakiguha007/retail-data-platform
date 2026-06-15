# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — `10_fact_audit_error.py`
# MAGIC
# MAGIC Audit-findings fact. One row per `sa_error` finding. CDF-driven from `silver.sa_error`.
# MAGIC This is the fact that powers the audit dashboard — Module 4's work made queryable.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Target** | `retaildp.gold_core.fact_audit_error` |
# MAGIC | **Grain** | One row per finding (`error_seq_no`) |
# MAGIC | **Source** | `silver.sa_error` (18 rules, ~3,000-3,500 findings) via CDF |
# MAGIC | **Merge key** | `error_seq_no` (= xxhash64(tran_seq_no, rule_id)) |
# MAGIC | **Clustering** | Liquid Clustering on `(business_date, store_key)` |
# MAGIC
# MAGIC ## FKs
# MAGIC `date_key`, `store_key`, `channel_key`. `rule_id` + `severity` are degenerate dimensions
# MAGIC (low cardinality — 18 rules, 3 severities; no separate dim needed for a portfolio piece).
# MAGIC
# MAGIC ## Measures
# MAGIC - `error_count` — always 1 (additive; SUM gives finding counts). Lets the mart/BI layer
# MAGIC   `SUM(error_count)` rather than `COUNT(*)`, which is cleaner in Power BI.
# MAGIC - `measured_value`, `expected_value`, `delta` — the rule's numbers
# MAGIC - `abs_delta` — `ABS(delta)`, the dollar impact magnitude for ranking findings
# MAGIC
# MAGIC ## Note on grain vs other facts
# MAGIC `sa_error.error_seq_no = xxhash64(tran_seq_no, rule_id)` is the natural key. A single
# MAGIC transaction can have multiple findings (one per rule it violates), each its own row.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 0. ⚠ One-time cleanup (only if rebuilding)

# COMMAND ----------

#spark.sql("DROP TABLE IF EXISTS retaildp.gold_core.fact_audit_error")
#spark.sql("DELETE FROM retaildp.gold_core._fact_load_log WHERE fact_table = 'fact_audit_error'")
#print("Dropped fact_audit_error + cleared watermark. Next run bootstraps.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration + helpers

# COMMAND ----------

CATALOG      = "retaildp"
CORE         = "gold_core"
FACT         = "fact_audit_error"
FACT_FQN     = f"{CATALOG}.{CORE}.{FACT}"
SOURCE_TABLE = f"{CATALOG}.silver.sa_error"

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
# MAGIC ## 2. Pre-flight — confirm source schema
# MAGIC
# MAGIC sa_error columns (verified): ERROR_SEQ_NO, TRAN_SEQ_NO, STORE, BUSINESS_DATE,
# MAGIC RTLOG_ORIG_SYS, RULE_ID, RULE_NAME, SEVERITY, MEASURED_VALUE, EXPECTED_VALUE, DELTA,
# MAGIC ERROR_DESC, _audit_ts, _audit_run_id.

# COMMAND ----------

print("=== sa_error schema ===")
display(spark.sql(f"DESCRIBE TABLE {SOURCE_TABLE}"))

print("\n=== Findings by rule + severity ===")
display(spark.sql(f"""
    SELECT RULE_ID, SEVERITY, COUNT(*) AS findings
    FROM {SOURCE_TABLE}
    GROUP BY RULE_ID, SEVERITY
    ORDER BY findings DESC
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Create `fact_audit_error`

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {FACT_FQN} (
    date_key         BIGINT     COMMENT 'FK dim_date (-1 = unknown)',
    store_key        BIGINT     COMMENT 'FK dim_store (-1 = unknown)',
    channel_key      BIGINT     COMMENT 'FK dim_channel (-1 = unknown)',

    error_seq_no     BIGINT     NOT NULL  COMMENT 'Finding surrogate = xxhash64(tran_seq_no, rule_id)',
    tran_seq_no      BIGINT               COMMENT 'Offending transaction (degenerate; nullable for non-tran rules)',
    business_date    DATE       NOT NULL  COMMENT 'Clustering key',
    rtlog_orig_sys   STRING               COMMENT 'Raw channel (degenerate)',
    rule_id          STRING     NOT NULL  COMMENT 'Audit rule code (degenerate dim)',
    rule_name        STRING               COMMENT 'Human-readable rule name',
    severity         STRING     NOT NULL  COMMENT 'W / M / F (ReSA severity)',

    error_count      INT        NOT NULL  COMMENT 'Always 1 — additive count measure',
    measured_value   DECIMAL(20,4)        COMMENT 'What the rule observed',
    expected_value   DECIMAL(20,4)        COMMENT 'What the rule expected',
    delta            DECIMAL(20,4)        COMMENT 'measured - expected',
    abs_delta        DECIMAL(20,4)        COMMENT 'ABS(delta) — impact magnitude for ranking',
    error_desc       STRING               COMMENT 'Human-readable finding detail',
    audit_run_id     STRING               COMMENT 'Which audit batch produced this finding',

    _load_ts         TIMESTAMP  NOT NULL  COMMENT 'When loaded/updated',
    _source          STRING               COMMENT 'Source table'
)
USING DELTA
CLUSTER BY (business_date, store_key)
TBLPROPERTIES (
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.autoOptimize.autoCompact'   = 'true'
)
COMMENT 'Audit-findings fact — one row per sa_error finding. CDF-driven from silver.sa_error. Grain error_seq_no.'
""")

print(f"{FACT_FQN} ready.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Read source via CDF + resolve watermark window

# COMMAND ----------

last_wm = get_watermark(FACT)
print(f"Current watermark for {FACT}: {last_wm}")

cdf_df, version_to = read_cdf_since(SOURCE_TABLE, last_wm)
version_from = last_wm + 1

src_count = cdf_df.count()
print(f"Window: v{version_from}..v{version_to}  |  source rows: {src_count:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Build fact rows — measures + dim keys

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.types import DecimalType

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
        )

        staged = keyed.select(
            F.col("date_key"),
            F.col("store_key"),
            F.col("channel_key"),
            F.col("ERROR_SEQ_NO").alias("error_seq_no"),
            F.col("TRAN_SEQ_NO").alias("tran_seq_no"),
            F.col("BUSINESS_DATE").alias("business_date"),
            F.col("RTLOG_ORIG_SYS").alias("rtlog_orig_sys"),
            F.col("RULE_ID").alias("rule_id"),
            F.col("RULE_NAME").alias("rule_name"),
            F.col("SEVERITY").alias("severity"),
            F.lit(1).alias("error_count"),
            F.col("MEASURED_VALUE").alias("measured_value"),
            F.col("EXPECTED_VALUE").alias("expected_value"),
            F.col("DELTA").alias("delta"),
            F.abs(F.col("DELTA")).cast(DecimalType(20, 4)).alias("abs_delta"),
            F.col("ERROR_DESC").alias("error_desc"),
            F.col("_audit_run_id").alias("audit_run_id"),
            F.current_timestamp().alias("_load_ts"),
            F.lit(SOURCE_TABLE).alias("_source"),
        ).where(F.col("error_seq_no").isNotNull())

        staged.createOrReplaceTempView("fact_audit_error_staged")
        clean_n = staged.count()
        print(f"Staged fact rows: {clean_n:,}")

    except Exception as e:
        fail_fact_run(run_id, str(e))
        raise

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. MERGE into the fact

# COMMAND ----------

if src_count > 0:
    try:
        before = spark.table(FACT_FQN).count()

        spark.sql(f"""
            MERGE INTO {FACT_FQN} AS tgt
            USING fact_audit_error_staged AS src
            ON tgt.error_seq_no = src.error_seq_no
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
# MAGIC ## 7. Validation

# COMMAND ----------

print("=== Findings by rule (fact) ===")
display(spark.sql(f"""
    SELECT rule_id, severity, SUM(error_count) AS findings
    FROM {FACT_FQN} GROUP BY rule_id, severity ORDER BY findings DESC
"""))

# COMMAND ----------

print("=== Orphan FK check ===")
display(spark.sql(f"""
    SELECT
        SUM(CASE WHEN date_key    = -1 THEN 1 ELSE 0 END) AS orphan_date,
        SUM(CASE WHEN store_key   = -1 THEN 1 ELSE 0 END) AS orphan_store,
        SUM(CASE WHEN channel_key = -1 THEN 1 ELSE 0 END) AS orphan_channel
    FROM {FACT_FQN}
"""))

# COMMAND ----------

print("=== PK uniqueness on error_seq_no ===")
display(spark.sql(f"""
    SELECT COUNT(*) AS duplicate_keys FROM (
        SELECT error_seq_no, COUNT(*) c FROM {FACT_FQN}
        GROUP BY error_seq_no HAVING COUNT(*) > 1
    )
"""))

# COMMAND ----------

print("=== Row reconciliation: fact == silver sa_error ===")
display(spark.sql(f"""
    SELECT
        (SELECT COUNT(*) FROM {SOURCE_TABLE}) AS silver_rows,
        (SELECT COUNT(*) FROM {FACT_FQN})     AS fact_rows
"""))

# COMMAND ----------

print("=== Findings by channel + severity ===")
display(spark.sql(f"""
    SELECT rtlog_orig_sys, severity, SUM(error_count) AS findings
    FROM {FACT_FQN} GROUP BY rtlog_orig_sys, severity
    ORDER BY rtlog_orig_sys, severity
"""))

# COMMAND ----------

print("=== Watermark log ===")
display(spark.sql(f"""
    SELECT run_id, version_from, version_to, rows_inserted, rows_updated, run_status
    FROM {CATALOG}.{CORE}._fact_load_log
    WHERE fact_table = '{FACT}' ORDER BY run_start_ts DESC LIMIT 5
"""))

# COMMAND ----------

print(f"✅ fact_audit_error load complete.")
print(f"   Fact rows:     {spark.table(FACT_FQN).count():,}")
print(f"   New watermark: v{version_to}")
