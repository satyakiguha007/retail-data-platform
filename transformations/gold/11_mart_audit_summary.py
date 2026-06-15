# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — `11_mart_audit_summary.py`
# MAGIC
# MAGIC Denormalized audit-findings mart. Powers the Audit Findings dashboard.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Target** | `retaildp.gold_marts.mart_audit_summary` |
# MAGIC | **Grain** | One row per `(business_date, store, channel, rule_id, severity)` |
# MAGIC | **Source** | `gold_core.fact_audit_error` + dims (labels, not keys) |
# MAGIC | **Refresh** | Full `INSERT OVERWRITE` — deterministic projection of gold_core |
# MAGIC | **Logged to** | `gold_core._mart_refresh_log` |
# MAGIC
# MAGIC ## Why full-overwrite (not incremental)
# MAGIC Marts are deterministic projections of a small core. Re-aggregating from scratch each
# MAGIC run is cheap and removes all incremental-state complexity. The production-ideal
# MAGIC "re-aggregate only touched store-days" pattern is noted in `_mart_refresh_log` design;
# MAGIC for this data volume, overwrite is simpler and equally correct.
# MAGIC
# MAGIC ## Label-rich, key-free
# MAGIC Marts carry descriptive labels (store_name, country, channel_name, rule_name, calendar
# MAGIC attributes) — Power BI slices on names. Surrogate keys stay in the core star.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration

# COMMAND ----------

import uuid as _uuid
from datetime import datetime

CATALOG    = "retaildp"
CORE       = "gold_core"
MARTS      = "gold_marts"
MART       = "mart_audit_summary"
MART_FQN   = f"{CATALOG}.{MARTS}.{MART}"
LOG_TABLE  = f"{CATALOG}.{CORE}._mart_refresh_log"

run_id = f"MR_{datetime.utcnow():%Y%m%d_%H%M%S}_{_uuid.uuid4().hex[:8]}"
print(f"Target: {MART_FQN}")
print(f"Run:    {run_id}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Create mart

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {MART_FQN} (
    business_date    DATE,
    year             INT,
    year_month       STRING,
    fiscal_year_name STRING,

    store            BIGINT,
    store_name       STRING,
    country_name     STRING,

    channel_code     STRING,
    channel_name     STRING,

    rule_id          STRING,
    rule_name        STRING,
    severity         STRING,
    severity_label   STRING,

    finding_count    BIGINT,
    total_abs_delta  DECIMAL(20,4),
    avg_abs_delta    DECIMAL(20,4),
    max_abs_delta    DECIMAL(20,4),

    _refreshed_ts    TIMESTAMP
)
USING DELTA
TBLPROPERTIES (
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.autoOptimize.autoCompact'   = 'true'
)
COMMENT 'BI mart — audit findings by date/store/channel/rule. Full-overwrite projection of fact_audit_error.'
""")

print(f"{MART_FQN} ready.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Log start

# COMMAND ----------

spark.sql(f"""
    INSERT INTO {LOG_TABLE}
        (run_id, mart_table, triggering_fact_run_id, affected_store_day_count,
         rows_deleted, rows_inserted, run_start_ts, run_end_ts, run_status, error_message)
    VALUES
        ('{run_id}', '{MART}', 'FULL_OVERWRITE', -1,
         NULL, NULL, current_timestamp(), NULL, 'RUNNING', NULL)
""")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Build + overwrite

# COMMAND ----------

try:
    spark.sql(f"""
        INSERT OVERWRITE {MART_FQN}
        SELECT
            d.full_date                          AS business_date,
            d.year,
            d.year_month,
            d.fiscal_year_name,

            st.store,
            st.store_name,
            st.country_name,

            ch.channel_code,
            ch.channel_name,

            f.rule_id,
            f.rule_name,
            f.severity,
            CASE f.severity
                WHEN 'F' THEN 'Fatal'
                WHEN 'M' THEN 'Minor'
                WHEN 'W' THEN 'Warning'
                ELSE f.severity
            END                                  AS severity_label,

            SUM(f.error_count)                            AS finding_count,
            CAST(SUM(f.abs_delta) AS DECIMAL(20,4))       AS total_abs_delta,
            CAST(AVG(f.abs_delta) AS DECIMAL(20,4))       AS avg_abs_delta,
            CAST(MAX(f.abs_delta) AS DECIMAL(20,4))       AS max_abs_delta,

            current_timestamp()                  AS _refreshed_ts
        FROM {CATALOG}.{CORE}.fact_audit_error f
        LEFT JOIN {CATALOG}.{CORE}.dim_date    d  ON f.date_key    = d.date_key
        LEFT JOIN {CATALOG}.{CORE}.dim_store   st ON f.store_key   = st.store_key
        LEFT JOIN {CATALOG}.{CORE}.dim_channel ch ON f.channel_key = ch.channel_key
        GROUP BY
            d.full_date, d.year, d.year_month, d.fiscal_year_name,
            st.store, st.store_name, st.country_name,
            ch.channel_code, ch.channel_name,
            f.rule_id, f.rule_name, f.severity
    """)

    rows = spark.table(MART_FQN).count()
    spark.sql(f"""
        UPDATE {LOG_TABLE}
        SET rows_inserted = {rows}, run_end_ts = current_timestamp(), run_status = 'SUCCEEDED'
        WHERE run_id = '{run_id}'
    """)
    print(f"✅ Overwrote {MART_FQN}: {rows:,} rows")

except Exception as e:
    msg = str(e)[:1000].replace("'", "''")
    spark.sql(f"""
        UPDATE {LOG_TABLE}
        SET run_end_ts = current_timestamp(), run_status = 'FAILED', error_message = '{msg}'
        WHERE run_id = '{run_id}'
    """)
    raise

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Validation

# COMMAND ----------

print("=== Findings reconciliation: mart total == fact total ===")
display(spark.sql(f"""
    SELECT
        (SELECT SUM(finding_count) FROM {MART_FQN})                       AS mart_total,
        (SELECT SUM(error_count) FROM {CATALOG}.{CORE}.fact_audit_error)  AS fact_total
"""))

# COMMAND ----------

print("=== By rule + severity ===")
display(spark.sql(f"""
    SELECT rule_id, severity_label, SUM(finding_count) AS findings,
           ROUND(SUM(total_abs_delta),2) AS total_impact
    FROM {MART_FQN} GROUP BY rule_id, severity_label ORDER BY findings DESC
"""))

# COMMAND ----------

print("=== By channel ===")
display(spark.sql(f"""
    SELECT channel_name, SUM(finding_count) AS findings
    FROM {MART_FQN} GROUP BY channel_name ORDER BY findings DESC
"""))

# COMMAND ----------

print(f"✅ mart_audit_summary complete: {spark.table(MART_FQN).count():,} rows")
