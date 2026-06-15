# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — `12_mart_channel_pnl.py`
# MAGIC
# MAGIC Channel P&L mart. Powers the Channel Comparison dashboard.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Target** | `retaildp.gold_marts.mart_channel_pnl` |
# MAGIC | **Grain** | One row per `(business_date, channel, category)` |
# MAGIC | **Source** | `gold_core.fact_sales_line` + dim_item (current) + dims |
# MAGIC | **Refresh** | Full `INSERT OVERWRITE` |
# MAGIC
# MAGIC ## Measures
# MAGIC gross/returns/net sales (USD), line + transaction counts, units, avg unit price.
# MAGIC Sales vs returns split on `qty` sign — same convention as fact_store_day.
# MAGIC
# MAGIC ## dim_item join
# MAGIC Joins the SCD2 dim on `item_key` (which already points at the current version, since
# MAGIC fact_sales_line resolved item_key via is_current). Category comes along for the slice.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration

# COMMAND ----------

import uuid as _uuid
from datetime import datetime

CATALOG    = "retaildp"
CORE       = "gold_core"
MARTS      = "gold_marts"
MART       = "mart_channel_pnl"
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
    business_date     DATE,
    year              INT,
    year_month        STRING,
    fiscal_year_name  STRING,

    channel_code      STRING,
    channel_name      STRING,
    is_marketplace    BOOLEAN,

    category          STRING,

    gross_sales_usd   DECIMAL(20,4),
    returns_usd       DECIMAL(20,4),
    net_sales_usd     DECIMAL(20,4),
    units_sold        DECIMAL(20,4),
    line_count        BIGINT,
    tran_count        BIGINT,
    avg_unit_usd      DECIMAL(20,4),

    _refreshed_ts     TIMESTAMP
)
USING DELTA
TBLPROPERTIES (
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.autoOptimize.autoCompact'   = 'true'
)
COMMENT 'BI mart — channel P&L by date/channel/category. Full-overwrite projection of fact_sales_line.'
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

            ch.channel_code,
            ch.channel_name,
            ch.is_marketplace,

            COALESCE(it.category, 'uncategorized') AS category,

            CAST(SUM(CASE WHEN f.qty > 0 THEN f.gross_amt_usd ELSE 0 END) AS DECIMAL(20,4)) AS gross_sales_usd,
            CAST(SUM(CASE WHEN f.qty < 0 THEN ABS(f.gross_amt_usd) ELSE 0 END) AS DECIMAL(20,4)) AS returns_usd,
            CAST(SUM(CASE WHEN f.qty > 0 THEN f.gross_amt_usd ELSE -ABS(f.gross_amt_usd) END) AS DECIMAL(20,4)) AS net_sales_usd,
            CAST(SUM(f.qty) AS DECIMAL(20,4))                  AS units_sold,
            COUNT(*)                                           AS line_count,
            COUNT(DISTINCT f.tran_seq_no)                      AS tran_count,
            CAST(AVG(f.unit_retail_usd) AS DECIMAL(20,4))      AS avg_unit_usd,

            current_timestamp()                  AS _refreshed_ts
        FROM {CATALOG}.{CORE}.fact_sales_line f
        LEFT JOIN {CATALOG}.{CORE}.dim_date    d  ON f.date_key    = d.date_key
        LEFT JOIN {CATALOG}.{CORE}.dim_channel ch ON f.channel_key = ch.channel_key
        LEFT JOIN {CATALOG}.{CORE}.dim_item    it ON f.item_key    = it.item_key
        GROUP BY
            d.full_date, d.year, d.year_month, d.fiscal_year_name,
            ch.channel_code, ch.channel_name, ch.is_marketplace,
            COALESCE(it.category, 'uncategorized')
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

print("=== Net sales reconciliation: mart == fact_sales_line ===")
display(spark.sql(f"""
    SELECT
        (SELECT ROUND(SUM(net_sales_usd),2) FROM {MART_FQN})                                       AS mart_net,
        (SELECT ROUND(SUM(CASE WHEN qty>0 THEN gross_amt_usd ELSE -ABS(gross_amt_usd) END),2)
         FROM {CATALOG}.{CORE}.fact_sales_line)                                                     AS fact_net
"""))

# COMMAND ----------

print("=== Channel P&L summary (USD) ===")
display(spark.sql(f"""
    SELECT channel_name,
           ROUND(SUM(gross_sales_usd),2) AS gross,
           ROUND(SUM(returns_usd),2)     AS returns,
           ROUND(SUM(net_sales_usd),2)   AS net,
           SUM(line_count)               AS lines
    FROM {MART_FQN} GROUP BY channel_name ORDER BY net DESC
"""))

# COMMAND ----------

print("=== Top 15 categories by net sales ===")
display(spark.sql(f"""
    SELECT category, channel_name, ROUND(SUM(net_sales_usd),2) AS net
    FROM {MART_FQN} GROUP BY category, channel_name ORDER BY net DESC LIMIT 15
"""))

# COMMAND ----------

print(f"✅ mart_channel_pnl complete: {spark.table(MART_FQN).count():,} rows")
