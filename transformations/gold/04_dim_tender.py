# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — `04_dim_tender.py`
# MAGIC
# MAGIC Conformed tender (payment type) dimension. Type 1.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Target** | `retaildp.gold_core.dim_tender` |
# MAGIC | **Grain** | One row per distinct `(tender_type_group, tender_type_id)` |
# MAGIC | **Natural key** | `(tender_type_group, tender_type_id)` composite |
# MAGIC | **Surrogate key** | `tender_key` (BIGINT, IDENTITY) |
# MAGIC | **SCD** | Type 1 |
# MAGIC | **Source** | `SELECT DISTINCT ... FROM silver.sa_tran_tender` (all 3 channels) |
# MAGIC
# MAGIC ## Actual silver schema (verified)
# MAGIC - `TENDER_TYPE_GROUP STRING NOT NULL`
# MAGIC - `TENDER_TYPE_ID  INTEGER NULLABLE`
# MAGIC
# MAGIC ## NULL handling for `tender_type_id`
# MAGIC `COALESCE(tender_type_id, -1)` in staging; dim column is `INT NOT NULL` with `-1` =
# MAGIC "unspecified in source". MERGE ON can then use plain `=`.
# MAGIC
# MAGIC ## Channel coverage note
# MAGIC Silver tender spans 3 channels:
# MAGIC - **POS** — ReSA-style groups (CASH, CREDIT, ...) likely UPPERCASE
# MAGIC - **MKT** — marketplace tender
# MAGIC - **OMS (Olist)** — from `olist_order_payments`: credit_card, boleto, voucher, debit_card
# MAGIC   (installment fan-out). These may arrive lowercase / underscored. The classification
# MAGIC   CASE below normalises with `UPPER()` and catches underscore variants.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 0. ⚠ One-time cleanup of any stale table
# MAGIC
# MAGIC If a `dim_tender` was created during an earlier run, it may carry the wrong
# MAGIC `tender_type_id` type (STRING instead of INT), which would break the MERGE the same way
# MAGIC `dim_store` did. Drop it once, then run cells 1→6. Harmless no-op on re-runs.

# COMMAND ----------

spark.sql("DROP TABLE IF EXISTS retaildp.gold_core.dim_tender")
print("Dropped stale dim_tender (if it existed). Now run cells 1→6.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration

# COMMAND ----------

CATALOG       = "retaildp"
SCHEMA        = "gold_core"
TABLE         = "dim_tender"
TABLE_FQN     = f"{CATALOG}.{SCHEMA}.{TABLE}"

SOURCE_TABLE  = f"{CATALOG}.silver.sa_tran_tender"

print(f"Target:  {TABLE_FQN}")
print(f"Source:  {SOURCE_TABLE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Discovery — distinct tender values + null check

# COMMAND ----------

print("=== Distinct (group, id) with counts ===")
display(spark.sql(f"""
    SELECT
        tender_type_group,
        tender_type_id,
        COUNT(*) AS row_count
    FROM {SOURCE_TABLE}
    GROUP BY tender_type_group, tender_type_id
    ORDER BY tender_type_group, tender_type_id NULLS FIRST
"""))

# COMMAND ----------

print("=== NULL tender_type_id check ===")
display(spark.sql(f"""
    SELECT
        SUM(CASE WHEN tender_type_id IS NULL THEN 1 ELSE 0 END) AS null_id_rows,
        COUNT(*)                                                AS total_rows
    FROM {SOURCE_TABLE}
"""))

print("\n=== Distinct groups only (eyeball for unfamiliar values) ===")
display(spark.sql(f"""
    SELECT DISTINCT tender_type_group
    FROM {SOURCE_TABLE}
    ORDER BY tender_type_group
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Create `dim_tender`

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {TABLE_FQN} (
    tender_key         BIGINT     GENERATED ALWAYS AS IDENTITY  COMMENT 'Surrogate key',
    tender_type_group  STRING     NOT NULL                      COMMENT 'ReSA tender group (CASH, CREDIT, ...) — natural key pt.1',
    tender_type_id     INT        NOT NULL                      COMMENT 'ReSA tender id; -1 = unspecified in source — natural key pt.2',
    tender_type_name   STRING     NOT NULL                      COMMENT 'Human-readable name for BI display',
    is_electronic      BOOLEAN    NOT NULL                      COMMENT 'Cards (incl. generic CARD), debit, mobile, PIX, UPI. VOUCHER and BOLETO excluded.',
    is_cash            BOOLEAN    NOT NULL                      COMMENT 'Physical cash only',
    is_credit          BOOLEAN    NOT NULL                      COMMENT 'Credit instruments (incur liability)',
    _ingest_ts         TIMESTAMP  NOT NULL  DEFAULT current_timestamp(),
    CONSTRAINT dim_tender_uk UNIQUE (tender_type_group, tender_type_id)
)
USING DELTA
TBLPROPERTIES (
    'delta.feature.allowColumnDefaults' = 'supported',
    'delta.autoOptimize.optimizeWrite'  = 'true',
    'delta.autoOptimize.autoCompact'    = 'true'
)
COMMENT 'Conformed tender dimension. Type 1. Distinct silver.sa_tran_tender across all channels. tender_type_id=-1 = unspecified.'
""")

print(f"{TABLE_FQN} ready.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Build staging — distinct tender + derived classification
# MAGIC
# MAGIC Classification normalises with `UPPER()` and a `REPLACE('_','')` so that `credit_card`,
# MAGIC `CREDIT_CARD`, and `CREDITCARD` all collapse to the same match key. Unknown groups fall
# MAGIC through to all-FALSE (safest). `boleto` (Brazilian bank slip) is intentionally
# MAGIC non-electronic, non-cash, non-credit.

# COMMAND ----------

staging = spark.sql(f"""
    WITH base AS (
        SELECT DISTINCT
            tender_type_group,
            COALESCE(tender_type_id, -1) AS tender_type_id,
            tender_type_id               AS raw_id,
            -- normalised match key: uppercase, strip underscores/spaces
            REPLACE(REPLACE(UPPER(tender_type_group), '_', ''), ' ', '') AS grp_norm
        FROM {SOURCE_TABLE}
        WHERE tender_type_group IS NOT NULL
    )
    SELECT
        tender_type_group,
        tender_type_id,

        CASE
            WHEN raw_id IS NULL
                THEN CONCAT(INITCAP(REPLACE(tender_type_group, '_', ' ')), ' (unspecified)')
            ELSE CONCAT(INITCAP(REPLACE(tender_type_group, '_', ' ')), ' - ', CAST(tender_type_id AS STRING))
        END AS tender_type_name,

        -- is_electronic: cards (any spelling), mobile/wallet, gift card, PIX, UPI.
        -- VOUCHER deliberately excluded — modelled as its own distinct ReSA tender class.
        CASE
            WHEN grp_norm IN (
                'CARD', 'CREDIT', 'CREDITCARD', 'DEBIT', 'DEBITCARD',
                'GIFTCARD', 'MOBILE', 'MOBILEPAY', 'STOREDVALUE',
                'PIX', 'UPI', 'WALLET'
            ) THEN TRUE
            ELSE FALSE
        END AS is_electronic,

        (grp_norm = 'CASH') AS is_cash,

        -- is_credit: credit instruments
        CASE
            WHEN grp_norm IN ('CREDIT', 'CREDITCARD') THEN TRUE
            ELSE FALSE
        END AS is_credit

    FROM base
""")

staging.createOrReplaceTempView("dim_tender_staging")

staging_count   = staging.count()
distinct_keys   = staging.select("tender_type_group", "tender_type_id").distinct().count()
print(f"Staging rows:    {staging_count}")
print(f"Distinct keys:   {distinct_keys}")
if staging_count != distinct_keys:
    print(f"⚠ Duplicate natural keys in staging! {staging_count - distinct_keys} dupe(s).")
else:
    print("✅ No duplicate natural keys.")

display(staging.orderBy("tender_type_group", "tender_type_id"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. MERGE (Type 1)

# COMMAND ----------

spark.sql(f"""
MERGE INTO {TABLE_FQN}    AS tgt
USING dim_tender_staging  AS src
ON  tgt.tender_type_group = src.tender_type_group
AND tgt.tender_type_id    = src.tender_type_id

WHEN MATCHED AND (
       (tgt.tender_type_name <=> src.tender_type_name) IS FALSE
    OR (tgt.is_electronic    <=> src.is_electronic)    IS FALSE
    OR (tgt.is_cash          <=> src.is_cash)          IS FALSE
    OR (tgt.is_credit        <=> src.is_credit)        IS FALSE
) THEN UPDATE SET
    tender_type_name = src.tender_type_name,
    is_electronic    = src.is_electronic,
    is_cash          = src.is_cash,
    is_credit        = src.is_credit,
    _ingest_ts       = current_timestamp()

WHEN NOT MATCHED THEN INSERT (
    tender_type_group, tender_type_id, tender_type_name,
    is_electronic, is_cash, is_credit
) VALUES (
    src.tender_type_group, src.tender_type_id, src.tender_type_name,
    src.is_electronic, src.is_cash, src.is_credit
)
""")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Validation

# COMMAND ----------

print("=== dim_tender contents ===")
display(spark.sql(f"""
    SELECT tender_key, tender_type_group, tender_type_id, tender_type_name,
           is_electronic, is_cash, is_credit
    FROM {TABLE_FQN}
    ORDER BY tender_type_group, tender_type_id
"""))

# COMMAND ----------

print("=== Classification distribution ===")
display(spark.sql(f"""
    SELECT
        COUNT(*)                          AS total,
        SUM(CAST(is_cash       AS INT))   AS cash_rows,
        SUM(CAST(is_credit     AS INT))   AS credit_rows,
        SUM(CAST(is_electronic AS INT))   AS electronic_rows
    FROM {TABLE_FQN}
"""))

# COMMAND ----------

print("=== Coverage: every silver tender combo has a dim_tender row ===")
missing = spark.sql(f"""
    SELECT
        s.tender_type_group,
        COALESCE(s.tender_type_id, -1) AS tender_type_id,
        COUNT(*) AS silver_row_count
    FROM {SOURCE_TABLE} s
    LEFT JOIN {TABLE_FQN} d
      ON  s.tender_type_group            = d.tender_type_group
      AND COALESCE(s.tender_type_id, -1) = d.tender_type_id
    WHERE d.tender_key IS NULL
    GROUP BY s.tender_type_group, COALESCE(s.tender_type_id, -1)
""")
if missing.count() > 0:
    print("❌ Silver tender combos with no dim_tender row:")
    display(missing)
else:
    print("✅ Every silver tender combo has a dim_tender row.")

# COMMAND ----------

print(f"✅ dim_tender complete.")
print(f"   Rows: {spark.table(TABLE_FQN).count()}")
