# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — `13_mart_store_day_scorecard.py`
# MAGIC
# MAGIC Store-day operations scorecard. Powers the Operations dashboard.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Target** | `retaildp.gold_marts.mart_store_day_scorecard` |
# MAGIC | **Grain** | One row per `(business_date, store, channel)` |
# MAGIC | **Source** | `gold_core.fact_store_day` + dims |
# MAGIC | **Refresh** | Full `INSERT OVERWRITE` |
# MAGIC
# MAGIC ## Wide, label-rich
# MAGIC This is the denormalized scorecard: store labels + calendar attributes + all the
# MAGIC store-day measures + derived KPIs (return rate, avg basket, error flag). One flat row
# MAGIC the ops dashboard binds directly.
# MAGIC
# MAGIC ## Derived KPIs added on top of fact_store_day
# MAGIC - `return_rate` = returns / gross (0 when gross 0)
# MAGIC - `avg_basket_usd` = net_sales / tran_count
# MAGIC - `avg_lines_per_tran` = line_count / tran_count
# MAGIC - `has_audit_findings` = error_count > 0
# MAGIC - `is_weekend`, `day_name` from dim_date for day-of-week analysis

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration

# COMMAND ----------

import uuid as _uuid
from datetime import datetime

CATALOG    = "retaildp"
CORE       = "gold_core"
MARTS      = "gold_marts"
MART       = "mart_store_day_scorecard"
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
    business_date       DATE,
    year                INT,
    year_month          STRING,
    fiscal_year_name    STRING,
    day_name            STRING,
    is_weekend          BOOLEAN,

    store               BIGINT,
    store_name          STRING,
    country_name        STRING,
    is_virtual          BOOLEAN,

    channel_code        STRING,
    channel_name        STRING,

    gross_sales_usd     DECIMAL(20,4),
    returns_usd         DECIMAL(20,4),
    net_sales_usd       DECIMAL(20,4),
    line_count          BIGINT,
    tran_count          BIGINT,
    error_count         BIGINT,

    -- derived KPIs
    return_rate         DECIMAL(9,4),
    avg_basket_usd      DECIMAL(20,4),
    avg_lines_per_tran  DECIMAL(12,4),
    has_audit_findings  BOOLEAN,

    _refreshed_ts       TIMESTAMP
)
USING DELTA
TBLPROPERTIES (
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.autoOptimize.autoCompact'   = 'true'
)
COMMENT 'BI mart — wide store-day scorecard with derived KPIs. Full-overwrite projection of fact_store_day.'
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
            d.day_name,
            d.is_weekend,

            st.store,
            st.store_name,
            st.country_name,
            st.is_virtual,

            ch.channel_code,
            ch.channel_name,

            f.gross_sales_usd,
            f.returns_usd,
            f.net_sales_usd,
            f.line_count,
            f.tran_count,
            f.error_count,

            -- derived KPIs (guard divide-by-zero)
            CAST(CASE WHEN f.gross_sales_usd > 0
                      THEN f.returns_usd / f.gross_sales_usd ELSE 0 END AS DECIMAL(9,4))   AS return_rate,
            CAST(CASE WHEN f.tran_count > 0
                      THEN f.net_sales_usd / f.tran_count ELSE 0 END AS DECIMAL(20,4))      AS avg_basket_usd,
            CAST(CASE WHEN f.tran_count > 0
                      THEN f.line_count / f.tran_count ELSE 0 END AS DECIMAL(12,4))         AS avg_lines_per_tran,
            (f.error_count > 0)                  AS has_audit_findings,

            current_timestamp()                  AS _refreshed_ts
        FROM {CATALOG}.{CORE}.fact_store_day f
        LEFT JOIN {CATALOG}.{CORE}.dim_date    d  ON f.date_key    = d.date_key
        LEFT JOIN {CATALOG}.{CORE}.dim_store   st ON f.store_key   = st.store_key
        LEFT JOIN {CATALOG}.{CORE}.dim_channel ch ON f.channel_key = ch.channel_key
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

print("=== Row count == fact_store_day ===")
display(spark.sql(f"""
    SELECT
        (SELECT COUNT(*) FROM {MART_FQN})                          AS mart_rows,
        (SELECT COUNT(*) FROM {CATALOG}.{CORE}.fact_store_day)     AS fact_rows
"""))

# COMMAND ----------

print("=== Net sales reconciliation ===")
display(spark.sql(f"""
    SELECT
        (SELECT ROUND(SUM(net_sales_usd),2) FROM {MART_FQN})                       AS mart_net,
        (SELECT ROUND(SUM(net_sales_usd),2) FROM {CATALOG}.{CORE}.fact_store_day)  AS fact_net
"""))

# COMMAND ----------

print("=== Day-of-week pattern (net sales by weekday) ===")
display(spark.sql(f"""
    SELECT day_name, is_weekend,
           ROUND(SUM(net_sales_usd),2) AS net, SUM(tran_count) AS trans
    FROM {MART_FQN} GROUP BY day_name, is_weekend
    ORDER BY net DESC
"""))

# COMMAND ----------

print("=== Store-days flagged with audit findings ===")
display(spark.sql(f"""
    SELECT channel_name,
           SUM(CASE WHEN has_audit_findings THEN 1 ELSE 0 END) AS flagged_store_days,
           COUNT(*) AS total_store_days
    FROM {MART_FQN} GROUP BY channel_name ORDER BY flagged_store_days DESC
"""))

# COMMAND ----------

print(f"✅ mart_store_day_scorecard complete: {spark.table(MART_FQN).count():,} rows")
