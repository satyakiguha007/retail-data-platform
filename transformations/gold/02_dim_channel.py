# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — `02_dim_channel.py`
# MAGIC
# MAGIC Conformed channel dimension. **Seed-only** (3 rows, hardcoded).
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Target** | `retaildp.gold_core.dim_channel` |
# MAGIC | **Grain** | One row per sales channel |
# MAGIC | **Natural key** | `channel_code` (STRING) |
# MAGIC | **Surrogate key** | `channel_key` (BIGINT, IDENTITY) |
# MAGIC | **SCD** | Type 1 — re-seed updates attributes in place |
# MAGIC | **Source** | Hardcoded seed (POS, MKT, OLIST) |
# MAGIC
# MAGIC ## Channel-code design (VERIFIED against silver)
# MAGIC The channel_code is the Power BI / LLM-facing identifier per the locked decision:
# MAGIC `POS`, `MKT`, `OLIST`. The `source_system_code` is the silver discriminator
# MAGIC (`RTLOG_ORIG_SYS`). Discovery confirmed silver actually stores:
# MAGIC `POS`, `MKT`, and **`OMS`** (not `OLIST`). So the Olist row maps
# MAGIC `channel_code='OLIST'` ← `source_system_code='OMS'`. Facts join
# MAGIC `rtlog_orig_sys = source_system_code` and surface the `OLIST` label.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration

# COMMAND ----------

CATALOG    = "retaildp"
SCHEMA     = "gold_core"
TABLE      = "dim_channel"
TABLE_FQN  = f"{CATALOG}.{SCHEMA}.{TABLE}"

print(f"Target: {TABLE_FQN}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Discovery — what RTLOG_ORIG_SYS values are actually in silver?
# MAGIC
# MAGIC Eyeball this before proceeding. If the values aren't `{POS, MKT, OLIST}`, adjust the
# MAGIC `source_system_code` literals in cell 4.

# COMMAND ----------

display(spark.sql(f"""
    SELECT rtlog_orig_sys, COUNT(*) AS row_count
    FROM {CATALOG}.silver.sa_tran_head
    GROUP BY rtlog_orig_sys
    ORDER BY rtlog_orig_sys
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Create `dim_channel`

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {TABLE_FQN} (
    channel_key          BIGINT     GENERATED ALWAYS AS IDENTITY  COMMENT 'Surrogate key',
    channel_code         STRING     NOT NULL                      COMMENT 'Natural key — short code (POS, MKT, OLIST)',
    channel_name         STRING     NOT NULL                      COMMENT 'Human-readable name',
    channel_description  STRING                                   COMMENT 'Longer description for BI tooltips',
    source_system_code   STRING     NOT NULL                      COMMENT 'Maps to silver.sa_tran_head.rtlog_orig_sys',
    is_online            BOOLEAN    NOT NULL,
    is_marketplace       BOOLEAN    NOT NULL,
    _ingest_ts           TIMESTAMP  NOT NULL  DEFAULT current_timestamp(),
    CONSTRAINT dim_channel_code_uk UNIQUE (channel_code)
)
USING DELTA
TBLPROPERTIES (
    'delta.feature.allowColumnDefaults' = 'supported',
    'delta.autoOptimize.optimizeWrite'  = 'true',
    'delta.autoOptimize.autoCompact'    = 'true'
)
COMMENT 'Conformed channel dimension. Seed-only. Each row maps to a value of silver.rtlog_orig_sys.'
""")

print(f"{TABLE_FQN} ready.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Build seed staging
# MAGIC
# MAGIC ⚠ If the discovery cell showed different `RTLOG_ORIG_SYS` values, edit the
# MAGIC `source_system_code` field below before running cell 5.

# COMMAND ----------

from pyspark.sql import Row

seed = [
    Row(channel_code="POS",   channel_name="Point of Sale",  channel_description="Physical retail store transactions via POS register",  source_system_code="POS",   is_online=False, is_marketplace=False),
    Row(channel_code="MKT",   channel_name="Marketplace",    channel_description="Third-party online marketplace channel",               source_system_code="MKT",   is_online=True,  is_marketplace=True),
    Row(channel_code="OLIST", channel_name="Olist Brazil",   channel_description="Brazilian e-commerce orders via Olist platform",       source_system_code="OMS",   is_online=True,  is_marketplace=True),
]

staging = spark.createDataFrame(seed)
staging.createOrReplaceTempView("dim_channel_staging")
display(staging)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. MERGE (Type 1)
# MAGIC
# MAGIC `<=>` is null-safe equality. `<=> ... IS FALSE` means "values differ accounting for NULLs".
# MAGIC Standard Type 1 idiom: update only when attributes actually changed, never on no-op re-runs.

# COMMAND ----------

spark.sql(f"""
MERGE INTO {TABLE_FQN}  AS tgt
USING dim_channel_staging AS src
ON tgt.channel_code = src.channel_code

WHEN MATCHED AND (
       (tgt.channel_name        <=> src.channel_name)        IS FALSE
    OR (tgt.channel_description <=> src.channel_description) IS FALSE
    OR (tgt.source_system_code  <=> src.source_system_code)  IS FALSE
    OR (tgt.is_online           <=> src.is_online)           IS FALSE
    OR (tgt.is_marketplace      <=> src.is_marketplace)      IS FALSE
) THEN UPDATE SET
    channel_name        = src.channel_name,
    channel_description = src.channel_description,
    source_system_code  = src.source_system_code,
    is_online           = src.is_online,
    is_marketplace      = src.is_marketplace,
    _ingest_ts          = current_timestamp()

WHEN NOT MATCHED THEN INSERT (
    channel_code, channel_name, channel_description,
    source_system_code, is_online, is_marketplace
) VALUES (
    src.channel_code, src.channel_name, src.channel_description,
    src.source_system_code, src.is_online, src.is_marketplace
)
""")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Validation

# COMMAND ----------

print("=== dim_channel contents ===")
display(spark.sql(f"SELECT * FROM {TABLE_FQN} ORDER BY channel_code"))

# COMMAND ----------

print("=== Coverage check: every silver channel has a dim_channel row ===")
display(spark.sql(f"""
    SELECT
        s.rtlog_orig_sys,
        d.channel_code,
        d.channel_name,
        CASE WHEN d.channel_key IS NULL THEN '❌ MISSING' ELSE '✅' END AS status
    FROM (SELECT DISTINCT rtlog_orig_sys FROM {CATALOG}.silver.sa_tran_head) s
    LEFT JOIN {TABLE_FQN} d
      ON s.rtlog_orig_sys = d.source_system_code
    ORDER BY s.rtlog_orig_sys
"""))

# COMMAND ----------

print(f"✅ dim_channel complete.")
print(f"   Rows: {spark.table(TABLE_FQN).count()}")
