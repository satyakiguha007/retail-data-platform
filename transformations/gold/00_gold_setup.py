# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — `00_gold_setup.py`
# MAGIC
# MAGIC Bootstrap notebook for the Gold layer. Idempotent — safe to re-run.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Creates schemas** | `retaildp.gold_core`, `retaildp.gold_marts` |
# MAGIC | **Creates tables** | `gold_core._fact_load_log`, `gold_core._mart_refresh_log` |
# MAGIC | **Enables CDF on** | 7 silver tables + `audit.sa_error` |
# MAGIC | **Idempotent** | Yes — `IF NOT EXISTS` and `SET TBLPROPERTIES` |
# MAGIC | **One-time** | Yes — schemas, log tables, CDF enable are all set-and-forget |
# MAGIC
# MAGIC ## Why two schemas
# MAGIC `gold_core` holds the Kimball star (`dim_*`, `fact_*`) — the queryable semantic layer for
# MAGIC ad-hoc SQL and the Module-5 LLM agent. `gold_marts` holds denormalized BI marts (`mart_*`),
# MAGIC one per Power BI dashboard page. Separation lets Power BI bind to `gold_marts` only and the
# MAGIC LLM agent bind to `gold_core` only, with appropriate grants on each.
# MAGIC
# MAGIC The pre-existing empty `retaildp.gold` schema is left in place — drop manually when ready
# MAGIC (`DROP SCHEMA retaildp.gold`).
# MAGIC
# MAGIC ## Why CDF on silver
# MAGIC Every fact in `gold_core` is CDF-driven from silver. CDF captures inserts AND ReSA-style
# MAGIC `_REV` corrections. Without CDF, Gold sees only inserts and silently misses the
# MAGIC reconciliation story this project exists to demonstrate. Prerequisite: silver tables must
# MAGIC exist (Pass-1/2/3 complete), and `audit.sa_error` must exist.
# MAGIC
# MAGIC ## Delta protocol note
# MAGIC Log tables use `DEFAULT current_timestamp()` on `_ingest_ts`, which requires the Delta
# MAGIC table feature `allowColumnDefaults`. The feature is enabled inline in `TBLPROPERTIES` at
# MAGIC CREATE time. This upgrades the table's Delta writer protocol version — older Delta writers
# MAGIC (DBR < 11.x) won't be able to write to these tables. Not an issue on serverless DBR 16+.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Widgets & configuration

# COMMAND ----------

# Audit table location can vary — override here if sa_error doesn't live in silver.
dbutils.widgets.text("audit_table", "retaildp.silver.sa_error", "Audit Error Table FQN")

CATALOG       = "retaildp"
CORE_SCHEMA   = "gold_core"
MARTS_SCHEMA  = "gold_marts"

# Managed locations — both schemas anchor under the existing `gold` ADLS container
# via the `ext_gold` external location credential, separated by sub-path.
GOLD_BASE_URL  = "abfss://gold@stretaildpsatyaki01.dfs.core.windows.net"
CORE_LOCATION  = f"{GOLD_BASE_URL}/core/"
MARTS_LOCATION = f"{GOLD_BASE_URL}/marts/"

# Source tables that need CDF enabled for incremental fact loads.
SILVER_CDF_TABLES = [
    "retaildp.silver.sa_tran_head",
    "retaildp.silver.sa_tran_item",
    "retaildp.silver.sa_tran_tender",
    "retaildp.silver.sa_tran_disc",
    "retaildp.silver.sa_tran_tax",
    "retaildp.silver.sa_tran_igtax",
    "retaildp.silver.sa_store_day",
]
AUDIT_TABLE = dbutils.widgets.get("audit_table")

print(f"Catalog:          {CATALOG}")
print(f"Core schema:      {CATALOG}.{CORE_SCHEMA}   →  {CORE_LOCATION}")
print(f"Marts schema:     {CATALOG}.{MARTS_SCHEMA}  →  {MARTS_LOCATION}")
print(f"CDF source count: {len(SILVER_CDF_TABLES) + 1} ({len(SILVER_CDF_TABLES)} silver + 1 audit)")
print(f"Audit table:      {AUDIT_TABLE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Create schemas
# MAGIC
# MAGIC Each schema declares its own managed location under the `gold` container. `ext_gold`
# MAGIC external location supplies the credential; both sub-paths inherit auth from it. Validation
# MAGIC uses `DESCRIBE SCHEMA EXTENDED` since `system.information_schema.schemata` doesn't expose
# MAGIC the storage location column in this DBR version.

# COMMAND ----------

spark.sql(f"USE CATALOG {CATALOG}")

spark.sql(f"""
CREATE SCHEMA IF NOT EXISTS {CATALOG}.{CORE_SCHEMA}
MANAGED LOCATION '{CORE_LOCATION}'
COMMENT 'Gold core — Kimball star schema (dim_*, fact_*). CDF-driven from silver.'
""")

spark.sql(f"""
CREATE SCHEMA IF NOT EXISTS {CATALOG}.{MARTS_SCHEMA}
MANAGED LOCATION '{MARTS_LOCATION}'
COMMENT 'Gold marts — denormalized BI marts (mart_*), one per dashboard page. Re-aggregated from gold_core.'
""")

# Verify schemas exist and confirm managed locations
for schema in [CORE_SCHEMA, MARTS_SCHEMA]:
    print(f"\n=== {CATALOG}.{schema} ===")
    display(spark.sql(f"DESCRIBE SCHEMA EXTENDED {CATALOG}.{schema}"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Create `_fact_load_log`
# MAGIC
# MAGIC Append-only operational log. One row per fact-builder run. Each fact's watermark is
# MAGIC `MAX(version_to)` over rows where `fact_table = <target>` and `run_status = 'SUCCEEDED'`.
# MAGIC Watermark advances only on success — failed runs are logged but don't move the cursor.

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.{CORE_SCHEMA}._fact_load_log (
    run_id           STRING     NOT NULL  COMMENT 'Format: FL_YYYYMMDD_HHMMSS_<short_uuid>',
    fact_table       STRING     NOT NULL  COMMENT 'Target fact table (e.g. fact_sales_line)',
    source_table     STRING     NOT NULL  COMMENT 'Silver/audit source (e.g. retaildp.silver.sa_tran_item)',
    version_from     BIGINT     NOT NULL  COMMENT 'Silver _commit_version start (= last_watermark + 1)',
    version_to       BIGINT     NOT NULL  COMMENT 'Silver _commit_version end (inclusive). Watermark advances here on SUCCEEDED.',
    rows_inserted    BIGINT,
    rows_updated     BIGINT,
    rows_deleted     BIGINT,
    run_start_ts     TIMESTAMP  NOT NULL,
    run_end_ts       TIMESTAMP,
    run_status       STRING     NOT NULL  COMMENT 'RUNNING | SUCCEEDED | FAILED',
    error_message    STRING,
    _ingest_ts       TIMESTAMP  NOT NULL  DEFAULT current_timestamp()
)
USING DELTA
TBLPROPERTIES (
    'delta.feature.allowColumnDefaults' = 'supported',
    'delta.autoOptimize.optimizeWrite'  = 'true',
    'delta.autoOptimize.autoCompact'    = 'true'
)
COMMENT 'Operational log — one row per gold fact-builder run. Watermark source for incremental CDF reads.'
""")

print("_fact_load_log ready.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Create `_mart_refresh_log`
# MAGIC
# MAGIC Each row links to the `_fact_load_log.run_id` that triggered the mart refresh. Marts are
# MAGIC deterministic projections of `gold_core` — re-aggregate any `(store, business_date)`
# MAGIC touched by an upstream fact batch.

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.{CORE_SCHEMA}._mart_refresh_log (
    run_id                    STRING     NOT NULL  COMMENT 'Format: MR_YYYYMMDD_HHMMSS_<short_uuid>',
    mart_table                STRING     NOT NULL  COMMENT 'Target mart table (e.g. mart_audit_summary)',
    triggering_fact_run_id    STRING     NOT NULL  COMMENT 'FK to _fact_load_log.run_id',
    affected_store_day_count  INT        NOT NULL  COMMENT 'Number of (store_key, date_key) tuples re-aggregated',
    rows_deleted              BIGINT,
    rows_inserted             BIGINT,
    run_start_ts              TIMESTAMP  NOT NULL,
    run_end_ts                TIMESTAMP,
    run_status                STRING     NOT NULL  COMMENT 'RUNNING | SUCCEEDED | FAILED',
    error_message             STRING,
    _ingest_ts                TIMESTAMP  NOT NULL  DEFAULT current_timestamp()
)
USING DELTA
TBLPROPERTIES (
    'delta.feature.allowColumnDefaults' = 'supported',
    'delta.autoOptimize.optimizeWrite'  = 'true',
    'delta.autoOptimize.autoCompact'    = 'true'
)
COMMENT 'Operational log — one row per gold mart refresh. Links to the fact run that triggered it.'
""")

print("_mart_refresh_log ready.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Enable CDF on silver + audit sources
# MAGIC
# MAGIC `SET TBLPROPERTIES` is idempotent. Missing tables are reported as soft warnings rather
# MAGIC than hard failures — the audit table location may differ across environments. Re-run after
# MAGIC fixing any missing-table issues.

# COMMAND ----------

def enable_cdf(table_fqn: str) -> dict:
    """Enable CDF on a table. Returns a status dict for the summary."""
    try:
        spark.sql(f"DESCRIBE TABLE {table_fqn}").count()
    except Exception as e:
        return {"table": table_fqn, "status": "MISSING", "detail": str(e).splitlines()[0][:200]}

    try:
        spark.sql(f"""
            ALTER TABLE {table_fqn}
            SET TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true')
        """)
        props = spark.sql(f"SHOW TBLPROPERTIES {table_fqn}").collect()
        cdf_value = next(
            (r["value"] for r in props if r["key"] == "delta.enableChangeDataFeed"),
            None,
        )
        return {
            "table":  table_fqn,
            "status": "ENABLED" if cdf_value == "true" else "UNEXPECTED",
            "detail": f"delta.enableChangeDataFeed={cdf_value}",
        }
    except Exception as e:
        return {"table": table_fqn, "status": "FAILED", "detail": str(e).splitlines()[0][:200]}


cdf_results = [enable_cdf(t) for t in SILVER_CDF_TABLES + [AUDIT_TABLE]]

import pandas as pd
results_df = pd.DataFrame(cdf_results)
# Cast all columns to string for Arrow display safety on UC serverless
results_df = results_df.astype(str)
display(results_df)

missing = [r for r in cdf_results if r["status"] == "MISSING"]
failed  = [r for r in cdf_results if r["status"] == "FAILED"]
enabled = sum(1 for r in cdf_results if r["status"] == "ENABLED")

if missing:
    print(f"\n⚠ {len(missing)} table(s) missing — CDF not enabled:")
    for r in missing:
        print(f"  - {r['table']}: {r['detail']}")

if failed:
    print(f"\n❌ {len(failed)} table(s) failed to set CDF:")
    for r in failed:
        print(f"  - {r['table']}: {r['detail']}")

print(f"\n✅ CDF enabled on {enabled} of {len(cdf_results)} tables.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Validation summary

# COMMAND ----------

print("=== Schemas ===")
display(spark.sql(f"""
SELECT schema_name, comment, schema_owner, created, last_altered
FROM system.information_schema.schemata
WHERE catalog_name = '{CATALOG}'
  AND schema_name IN ('{CORE_SCHEMA}', '{MARTS_SCHEMA}')
ORDER BY schema_name
"""))

# COMMAND ----------

# DBTITLE 1,Cell 14
print("=== Operational log tables ===")
display(spark.sql(f"""
SELECT table_schema, table_name, table_type, comment
FROM system.information_schema.tables
WHERE table_catalog = '{CATALOG}'
  AND table_schema  = '{CORE_SCHEMA}'
  AND table_name LIKE '!_%' ESCAPE '!'
ORDER BY table_name
"""))

# COMMAND ----------

print("=== Log table row counts (both should be 0 on first run) ===")
display(spark.sql(f"""
  SELECT '_fact_load_log'    AS log_table, COUNT(*) AS row_count
  FROM {CATALOG}.{CORE_SCHEMA}._fact_load_log
  UNION ALL
  SELECT '_mart_refresh_log', COUNT(*)
  FROM {CATALOG}.{CORE_SCHEMA}._mart_refresh_log
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Done
# MAGIC
# MAGIC Next steps:
# MAGIC 1. Build `_shared/cdf_reader.py`, `_shared/watermark.py`, `_shared/dim_lookup.py`
# MAGIC 2. Build dims `01_dim_date.py` → `06_dim_customer.py`
# MAGIC 3. Build facts `07_fact_sales_line.py` → `10_fact_audit_error.py`
# MAGIC 4. Validation `99_gold_core_validation.py`
# MAGIC 5. Marts `11_mart_audit_summary.py` → `14_mart_payment_mix.py`

# COMMAND ----------

print("✅ Gold setup complete.")
print(f"   Schemas:     {CATALOG}.{CORE_SCHEMA}, {CATALOG}.{MARTS_SCHEMA}")
print(f"   Log tables:  _fact_load_log, _mart_refresh_log")
print(f"   CDF tables:  {enabled} enabled, {len(missing)} missing, {len(failed)} failed")
