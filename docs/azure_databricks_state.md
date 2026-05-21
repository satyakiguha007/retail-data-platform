# Azure + Databricks State — Cloud Configuration Reference

**Last verified:** 2026-05-20
**Owner:** Satyaki Guha
**Purpose:** Single source of truth for the cloud infrastructure provisioned for the
Retail Data Platform. Load this at session start so architecture is never re-explained.

---

## 1. Azure resources

| Resource | Name | Notes |
|---|---|---|
| Storage account (ADLS Gen2) | `stretaildpsatyaki01` | Hierarchical namespace enabled |
| Databricks workspace | `dbw-retaildp-001` | **Serverless workspace** — Unity Catalog enabled |
| Access connector for Databricks | (managed identity) | Granted **Storage Blob Data Contributor** at storage-account scope |
| Service principal (local-only) | `sp-retaildp-simulator-001` | Used by `adls_sync_console/` for local→cloud uploads via `.env` |

Two auth paths to the same storage account — both intentional, both correct:

| Path | Auth | Used by |
|---|---|---|
| Local sync console → ADLS `raw/` | Service principal + secret (`.env`) | `adls_sync_console/` |
| Databricks → ADLS (any container) | Access connector + managed identity via Unity Catalog | All notebooks, jobs, DLT pipelines |

---

## 2. ADLS Gen2 containers (8 total)

All under storage account `stretaildpsatyaki01`. The access connector's managed identity has
Storage Blob Data Contributor on each.

| Container | Role | UC mapping | Lifecycle |
|---|---|---|---|
| `raw` | Landing zone for ingested files (NDJSON, CSV) | External location only | Append-only, source of truth |
| `bronze` | Delta tables — raw conformed | Managed location of `retaildp.bronze` | Managed by UC |
| `silver` | Delta tables — ReSA SA_* canonical | Managed location of `retaildp.silver` | Managed by UC |
| `gold` | Delta tables — Kimball star | Managed location of `retaildp.gold` | Managed by UC |
| `quarantine` | Rejected rows from any layer | Managed location of `retaildp.quarantine` | Managed by UC |
| `checkpoints` | Auto Loader + Structured Streaming state | External location only (path use) | Job-state, not data |
| `artifacts` | MLflow models, Power BI exports, generated files | External location only (path use) | Path-only |
| `$logs` | Azure system | — | Ignore |

---

## 3. Unity Catalog configuration

### 3.1 Catalogs

| Catalog | Status | Use |
|---|---|---|
| `dbw_retaildp_001` | Auto-created on workspace provisioning | **Unused** — kept around but no tables |
| `retaildp` | Active project catalog | **All project work goes here** |

### 3.2 Storage credential

A single credential wraps the access connector. The same credential backs all 7 external locations.

### 3.3 External locations (7)

| Name | URL | Used by |
|---|---|---|
| `ext_raw` | `abfss://raw@stretaildpsatyaki01.dfs.core.windows.net/` | Bronze readers (Auto Loader, MERGE jobs) |
| `ext_bronze` | `abfss://bronze@stretaildpsatyaki01.dfs.core.windows.net/` | `retaildp.bronze` managed schema |
| `ext_silver` | `abfss://silver@stretaildpsatyaki01.dfs.core.windows.net/` | `retaildp.silver` managed schema |
| `ext_gold` | `abfss://gold@stretaildpsatyaki01.dfs.core.windows.net/` | `retaildp.gold` managed schema |
| `ext_quarantine` | `abfss://quarantine@stretaildpsatyaki01.dfs.core.windows.net/` | `retaildp.quarantine` managed schema |
| `ext_checkpoints` | `abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/` | Auto Loader `checkpointLocation` + `schemaLocation` |
| `ext_artifacts` | `abfss://artifacts@stretaildpsatyaki01.dfs.core.windows.net/` | MLflow, Power BI export targets |

### 3.4 Schemas in `retaildp`

```sql
USE CATALOG retaildp;
SHOW SCHEMAS;
-- bronze, silver, gold, quarantine, information_schema
```

| Schema | Managed location | Naming convention |
|---|---|---|
| `retaildp.bronze` | `abfss://bronze@.../` | One table per raw source: `fx_rates`, `weather`, `olist_*`, `pos_rtlog`, `marketplace` |
| `retaildp.silver` | `abfss://silver@.../` | ReSA canonical: `sa_tran_head`, `sa_tran_item`, `sa_tran_disc`, `sa_tran_tender`, `sa_tran_tax`, `sa_tran_igtax`, `sa_store_day`, `sa_store_data` |
| `retaildp.gold` | `abfss://gold@.../` | Kimball: `dim_*`, `fact_*` |
| `retaildp.quarantine` | `abfss://quarantine@.../` | Source-prefixed: `pos_rejects`, `marketplace_rejects`, `silver_sa_tran_head_rejects`, etc. Every table has a `rejection_reason` column |

All schemas are owned by `satyakiguha007@gmail.com`.

---

## 4. Compute model — Serverless only

The workspace is configured serverless. **No classic clusters used or needed.**

| Compute | Type | Used for |
|---|---|---|
| SQL Warehouse (Serverless Starter Warehouse) | Serverless SQL, 2X-Small | SQL Editor, ad-hoc queries, dashboards |
| Serverless Notebook Compute | Serverless Python/Scala/R/SQL | All PySpark notebooks (Bronze, Silver, Gold), Auto Loader, Structured Streaming |

### 4.1 Implications for notebook code

- **Library installs**: `%pip install <pkg>` at notebook scope — does NOT persist across sessions. Reinstall at top of notebook each time, or pin versions in a setup cell.
- **`dbutils.fs`**: some operations restricted on serverless. Prefer `spark.read.format(...).load(path)` and `spark.write.save(path)` over `dbutils.fs.cp` where possible.
- **Cluster-level Spark conf**: don't use `spark.conf.set("spark.databricks...")` for cluster-wide settings — set at session scope instead.
- **Auto Loader**: fully supported. `cloudFiles.schemaLocation` and `checkpointLocation` should point at `ext_checkpoints` paths.
- **Structured Streaming**: supported with some restrictions vs classic. For Bronze append-only patterns it works cleanly.
- **No autotermination setting** — serverless auto-releases after ~10 min idle.
- **Per-second billing** — no cost when idle. Cheaper for dev/portfolio work than classic clusters.

### 4.2 UC serverless restrictions encountered (and resolved)

| Function / operation | Status on UC serverless | Use instead |
|---|---|---|
| `input_file_name()` | ❌ Blocked (`UC_COMMAND_NOT_SUPPORTED`) | `col("_metadata.file_path")` |
| RDD `.rdd.map(...)` patterns | ❌ Blocked | DataFrame transformations only |
| `spark._jvm` / `_jsparkSession` JVM access | ❌ Blocked | Pure PySpark APIs |
| `spark.sparkContext.setJobGroup(...)` | ⚠️ Restricted | Skip; rely on cell-level tagging in UI |

The `_metadata` struct is available on all file-source DataFrames (CSV, JSON, Parquet, Avro). Its fields: