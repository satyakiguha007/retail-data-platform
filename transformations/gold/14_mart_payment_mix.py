# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — `14_mart_payment_mix.py`
# MAGIC
# MAGIC Payment-mix mart. Powers the Payment Analysis page.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Target** | `retaildp.gold_marts.mart_payment_mix` |
# MAGIC | **Grain** | One row per `(business_date, channel, tender_type_group)` |
# MAGIC | **Source** | `gold_core.fact_tender` + dim_tender + dims |
# MAGIC | **Refresh** | Full `INSERT OVERWRITE` |
# MAGIC
# MAGIC ## Measures
# MAGIC tender amount (USD), tender line count, plus the dim_tender classification flags
# MAGIC (is_cash / is_credit / is_electronic) carried as labels for the payment-type slice.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration

# COMMAND ----------

import uuid as _uuid
from datetime import datetime

CATALOG    = "retaildp"
CORE       = "gold_core"
MARTS      = "gold_marts"
MART       = "mart_payment_mix"
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
    business_date      DATE,
    year               INT,
    year_month         STRING,
    fiscal_year_name   STRING,

    channel_code       STRING,
    channel_name       STRING,

    tender_type_group  STRING,
    is_cash            BOOLEAN,
    is_credit          BOOLEAN,
    is_electronic      BOOLEAN,

    tender_amt_usd     DECIMAL(20,4),
    tender_line_count  BIGINT,
    tran_count         BIGINT,

    _refreshed_ts      TIMESTAMP
)
USING DELTA
TBLPROPERTIES (
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.autoOptimize.autoCompact'   = 'true'
)
COMMENT 'BI mart — payment mix by date/channel/tender group. Full-overwrite projection of fact_tender.'
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
# MAGIC
# MAGIC Join dim_tender for the classification flags. Group by the group (not individual
# MAGIC tender_type_id) — the payment page slices on cash/card/voucher buckets.

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

            t.tender_type_group,
            MAX(t.is_cash)       AS is_cash,
            MAX(t.is_credit)     AS is_credit,
            MAX(t.is_electronic) AS is_electronic,

            CAST(SUM(f.tender_amt_usd) AS DECIMAL(20,4)) AS tender_amt_usd,
            COUNT(*)                                     AS tender_line_count,
            COUNT(DISTINCT f.tran_seq_no)                AS tran_count,

            current_timestamp()                  AS _refreshed_ts
        FROM {CATALOG}.{CORE}.fact_tender f
        LEFT JOIN {CATALOG}.{CORE}.dim_date    d  ON f.date_key    = d.date_key
        LEFT JOIN {CATALOG}.{CORE}.dim_channel ch ON f.channel_key = ch.channel_key
        LEFT JOIN {CATALOG}.{CORE}.dim_tender  t  ON f.tender_key  = t.tender_key
        GROUP BY
            d.full_date, d.year, d.year_month, d.fiscal_year_name,
            ch.channel_code, ch.channel_name,
            t.tender_type_group
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

print("=== Tender reconciliation: mart == fact_tender (USD) ===")
display(spark.sql(f"""
    SELECT
        (SELECT ROUND(SUM(tender_amt_usd),2) FROM {MART_FQN})                     AS mart_amt,
        (SELECT ROUND(SUM(tender_amt_usd),2) FROM {CATALOG}.{CORE}.fact_tender)   AS fact_amt
"""))

# COMMAND ----------

print("=== Payment mix by channel + group (USD) ===")
display(spark.sql(f"""
    SELECT channel_name, tender_type_group,
           ROUND(SUM(tender_amt_usd),2) AS amt_usd,
           SUM(tender_line_count)       AS lines
    FROM {MART_FQN} GROUP BY channel_name, tender_type_group
    ORDER BY channel_name, amt_usd DESC
"""))

# COMMAND ----------

print("=== Cash vs electronic vs credit split (USD) ===")
display(spark.sql(f"""
    SELECT
        ROUND(SUM(CASE WHEN is_cash       THEN tender_amt_usd ELSE 0 END),2) AS cash_usd,
        ROUND(SUM(CASE WHEN is_credit     THEN tender_amt_usd ELSE 0 END),2) AS credit_usd,
        ROUND(SUM(CASE WHEN is_electronic THEN tender_amt_usd ELSE 0 END),2) AS electronic_usd,
        ROUND(SUM(tender_amt_usd),2)                                         AS total_usd
    FROM {MART_FQN}
"""))

# COMMAND ----------

print(f"✅ mart_payment_mix complete: {spark.table(MART_FQN).count():,} rows")
