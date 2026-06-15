# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — `03_dim_store.py`
# MAGIC
# MAGIC Conformed store dimension. Type 1.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Target** | `retaildp.gold_core.dim_store` |
# MAGIC | **Grain** | One row per store |
# MAGIC | **Natural key** | `store` (BIGINT) |
# MAGIC | **Surrogate key** | `store_key` (BIGINT, IDENTITY) |
# MAGIC | **SCD** | Type 1 |
# MAGIC | **Source** | `silver.sa_store_data` — **all 27 rows, including the OLIST virtual store** |
# MAGIC
# MAGIC ## IMPORTANT — silver is the single source of truth
# MAGIC `silver.sa_store_data` already contains **27 rows**: 26 physical stores + the virtual
# MAGIC Olist store (`store=99999`, `OLIST_BR`, `BRA`, `BRL`). This was added upstream during
# MAGIC Pass-3 (Olist) silver build. **This notebook does NOT synthesize the Olist row** — doing
# MAGIC so produced a duplicate 99999 in the earlier broken version. We read straight from silver
# MAGIC and derive `is_virtual` from the store number.
# MAGIC
# MAGIC ## Actual silver schema (verified)
# MAGIC `STORE`, `STORE_NAME`, `REGISTER_COUNT`, `COUNTRY` (ISO3), `CURRENCY_CODE`, `_silver_ts`,
# MAGIC `_source`. No address / city / state / postal.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 0. ⚠ One-time cleanup of the broken table
# MAGIC
# MAGIC The earlier broken version of this notebook created `dim_store` with the wrong columns
# MAGIC (`store_address`, `city`, `state`, `postal_code`) and possibly a duplicate 99999 row.
# MAGIC The error `Cannot resolve register_count in UPDATE` means the stale table is still on
# MAGIC disk. **Drop it once**, then run the rest of the notebook from cell 1.
# MAGIC
# MAGIC After it runs clean once, this cell is a harmless no-op on re-runs (table gets recreated
# MAGIC by cell 3).

# COMMAND ----------

# Run this ONCE to clear the stale-schema table, then re-run the whole notebook.
spark.sql("DROP TABLE IF EXISTS retaildp.gold_core.dim_store")
print("Dropped stale dim_store (if it existed). Now run cells 1→7.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration

# COMMAND ----------

CATALOG      = "retaildp"
SCHEMA       = "gold_core"
TABLE        = "dim_store"
TABLE_FQN    = f"{CATALOG}.{SCHEMA}.{TABLE}"

SOURCE_TABLE = f"{CATALOG}.silver.sa_store_data"
VIRTUAL_STORE_NO = 99999   # the Olist virtual store, already present in silver

print(f"Target:  {TABLE_FQN}")
print(f"Source:  {SOURCE_TABLE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Discovery — confirm silver content + the single 99999 row

# COMMAND ----------

print(f"=== {SOURCE_TABLE} schema ===")
display(spark.sql(f"DESCRIBE TABLE {SOURCE_TABLE}"))

# COMMAND ----------

print(f"=== Row count (expect 27) ===")
print(spark.table(SOURCE_TABLE).count(), "rows")

print(f"\n=== The virtual store (expect exactly ONE row for {VIRTUAL_STORE_NO}) ===")
display(spark.sql(f"""
    SELECT store, store_name, country, currency_code, register_count
    FROM {SOURCE_TABLE}
    WHERE store = {VIRTUAL_STORE_NO}
"""))

# COMMAND ----------

print("=== Country distribution ===")
display(spark.sql(f"""
    SELECT country, currency_code, COUNT(*) AS store_count
    FROM {SOURCE_TABLE}
    GROUP BY country, currency_code
    ORDER BY country
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Create `dim_store`

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {TABLE_FQN} (
    store_key       BIGINT     GENERATED ALWAYS AS IDENTITY  COMMENT 'Surrogate key',
    store           BIGINT     NOT NULL                      COMMENT 'Natural key — ReSA store number',
    store_name      STRING     NOT NULL,
    country_code    STRING     NOT NULL                      COMMENT 'ISO 3166 alpha-3 (IND, USA, GBR, ARE, SGP, BRA)',
    country_name    STRING                                   COMMENT 'Derived from country_code via lookup',
    currency_code   STRING     NOT NULL                      COMMENT 'ISO 4217 (INR, USD, GBP, AED, SGD, BRL)',
    register_count  INT        NOT NULL                      COMMENT 'Number of POS tills; low/zero for the virtual store',
    is_virtual      BOOLEAN    NOT NULL                      COMMENT 'TRUE for the Olist virtual store (99999); FALSE for physical',
    _ingest_ts      TIMESTAMP  NOT NULL  DEFAULT current_timestamp(),
    CONSTRAINT dim_store_uk UNIQUE (store)
)
USING DELTA
TBLPROPERTIES (
    'delta.feature.allowColumnDefaults' = 'supported',
    'delta.autoOptimize.optimizeWrite'  = 'true',
    'delta.autoOptimize.autoCompact'    = 'true'
)
COMMENT 'Conformed store dimension. Type 1. Sourced entirely from silver.sa_store_data (27 rows incl. Olist virtual store).'
""")

print(f"{TABLE_FQN} ready.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Build staging — read all silver stores, derive is_virtual + country_name
# MAGIC
# MAGIC No synthesis. `is_virtual` is derived from the store number. The OLIST row is read from
# MAGIC silver like any other store.

# COMMAND ----------

from pyspark.sql import functions as F

# Country code → country name lookup (extend if new countries appear)
country_lookup = spark.createDataFrame(
    [
        ("IND", "India"),
        ("USA", "United States"),
        ("GBR", "United Kingdom"),
        ("ARE", "United Arab Emirates"),
        ("SGP", "Singapore"),
        ("BRA", "Brazil"),
    ],
    ["country_code_lk", "country_name"],
)

staging = (
    spark.sql(f"""
        SELECT
            store,
            store_name,
            country         AS country_code,
            currency_code,
            register_count,
            (store = {VIRTUAL_STORE_NO}) AS is_virtual
        FROM {SOURCE_TABLE}
    """)
    .join(country_lookup, F.col("country_code") == F.col("country_code_lk"), "left")
    .drop("country_code_lk")
    .select("store", "store_name", "country_code", "country_name",
            "currency_code", "register_count", "is_virtual")
)

staging.createOrReplaceTempView("dim_store_staging")

staging_count = staging.count()
distinct_stores = staging.select("store").distinct().count()
print(f"Staging rows:     {staging_count}")
print(f"Distinct stores:  {distinct_stores}")
if staging_count != distinct_stores:
    print(f"⚠ DUPLICATE store numbers in staging! {staging_count - distinct_stores} dupe(s).")
else:
    print("✅ No duplicate store numbers.")

display(staging.orderBy("store"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Check for unmapped country codes

# COMMAND ----------

unmapped = spark.sql("""
    SELECT DISTINCT country_code
    FROM dim_store_staging
    WHERE country_name IS NULL AND country_code IS NOT NULL
""")

if unmapped.count() > 0:
    print("⚠ Unmapped country code(s) — extend country_lookup in cell 4 and re-run:")
    display(unmapped)
else:
    print("✅ All country codes resolved to names.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. MERGE (Type 1)

# COMMAND ----------

spark.sql(f"""
MERGE INTO {TABLE_FQN}  AS tgt
USING dim_store_staging AS src
ON tgt.store = src.store

WHEN MATCHED AND (
       (tgt.store_name     <=> src.store_name)     IS FALSE
    OR (tgt.country_code   <=> src.country_code)   IS FALSE
    OR (tgt.country_name   <=> src.country_name)   IS FALSE
    OR (tgt.currency_code  <=> src.currency_code)  IS FALSE
    OR (tgt.register_count <=> src.register_count) IS FALSE
    OR (tgt.is_virtual     <=> src.is_virtual)     IS FALSE
) THEN UPDATE SET
    store_name     = src.store_name,
    country_code   = src.country_code,
    country_name   = src.country_name,
    currency_code  = src.currency_code,
    register_count = src.register_count,
    is_virtual     = src.is_virtual,
    _ingest_ts     = current_timestamp()

WHEN NOT MATCHED THEN INSERT (
    store, store_name, country_code, country_name,
    currency_code, register_count, is_virtual
) VALUES (
    src.store, src.store_name, src.country_code, src.country_name,
    src.currency_code, src.register_count, src.is_virtual
)
""")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Validation

# COMMAND ----------

print("=== Row count + virtual presence (expect total=27, virtual=1) ===")
display(spark.sql(f"""
    SELECT
        COUNT(*)                            AS total_rows,
        COUNT(DISTINCT store)               AS distinct_stores,
        SUM(CAST(is_virtual AS INT))        AS virtual_rows,
        SUM(CAST(NOT is_virtual AS INT))    AS physical_rows
    FROM {TABLE_FQN}
"""))

# COMMAND ----------

print("=== By country ===")
display(spark.sql(f"""
    SELECT country_code, country_name, currency_code,
           COUNT(*) AS store_count, SUM(register_count) AS total_registers
    FROM {TABLE_FQN}
    GROUP BY country_code, country_name, currency_code
    ORDER BY country_code
"""))

# COMMAND ----------

print("=== The virtual store row ===")
display(spark.sql(f"""
    SELECT store_key, store, store_name, country_code, currency_code, register_count, is_virtual
    FROM {TABLE_FQN}
    WHERE is_virtual = TRUE
"""))

# COMMAND ----------

print("=== Coverage: every store transacting in silver has a dim_store row ===")
missing = spark.sql(f"""
    SELECT s.store, COUNT(*) AS silver_tran_count
    FROM {CATALOG}.silver.sa_tran_head s
    LEFT JOIN {TABLE_FQN} d ON s.store = d.store
    WHERE d.store_key IS NULL
    GROUP BY s.store
    ORDER BY s.store
""")
if missing.count() > 0:
    print("❌ Stores transacting in silver with no dim_store row:")
    display(missing)
else:
    print("✅ Every transacting store has a dim_store row.")

# COMMAND ----------

print(f"✅ dim_store complete.")
print(f"   Rows: {spark.table(TABLE_FQN).count()}")
