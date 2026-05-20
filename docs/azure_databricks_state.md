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
| Databricks workspace | `dbw-retaildp-001` | Premium tier, Unity Catalog enabled |
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

A single credential wraps the access connector. The same credential backs all 7 external
locations. (Exact name visible via `SHOW STORAGE CREDENTIALS`.)

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
| `retaildp.bronze` | `abfss://bronze@.../` | One table per raw source: `pos_rtlog`, `marketplace`, `olist_*`, `fx_rates`, `weather` |
| `retaildp.silver` | `abfss://silver@.../` | ReSA canonical: `sa_tran_head`, `sa_tran_item`, `sa_tran_disc`, `sa_tran_tender`, `sa_tran_tax`, `sa_tran_igtax`, `sa_store_day`, `sa_store_data` |
| `retaildp.gold` | `abfss://gold@.../` | Kimball: `dim_*`, `fact_*` |
| `retaildp.quarantine` | `abfss://quarantine@.../` | Source-prefixed: `pos_rejects`, `marketplace_rejects`, `silver_sa_tran_head_rejects`, etc. Every table has a `rejection_reason` column |

All schemas are owned by `satyakiguha007@gmail.com`.

---

## 4. Standard path references (for use in code)

```python
# Top of every Databricks notebook
RAW         = "abfss://raw@stretaildpsatyaki01.dfs.core.windows.net/"
BRONZE      = "abfss://bronze@stretaildpsatyaki01.dfs.core.windows.net/"
SILVER      = "abfss://silver@stretaildpsatyaki01.dfs.core.windows.net/"
GOLD        = "abfss://gold@stretaildpsatyaki01.dfs.core.windows.net/"
QUARANTINE  = "abfss://quarantine@stretaildpsatyaki01.dfs.core.windows.net/"
CHECKPOINTS = "abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/"
ARTIFACTS   = "abfss://artifacts@stretaildpsatyaki01.dfs.core.windows.net/"

CATALOG     = "retaildp"
```

Per-source raw paths follow the patterns established by Modules 1 + 2:

```
RAW + "pos/store=<n>/date=<YYYY-MM-DD>/hour=<HH>/rtlog.ndjson"
RAW + "marketplace/marketplace=<NAME>/date=<YYYY-MM-DD>/feed.ndjson"
RAW + "olist/<file>.csv"
RAW + "fx-rates/<file>.csv"
RAW + "weather/<file>.csv"
```

Checkpoint paths follow the per-source pattern:

```
CHECKPOINTS + "<source>/schema/"     # cloudFiles.schemaLocation
CHECKPOINTS + "<source>/state/"      # checkpointLocation
```

---

## 5. Updated convention — quarantine

The original `CLAUDE.md` referenced `silver._quarantine` as the location for rejected rows.
**This is superseded.** The new convention:

- Rejected rows go to the top-level `retaildp.quarantine` schema
- Table names are source-prefixed: `pos_rejects`, `silver_sa_tran_head_rejects`, etc.
- Every quarantine table has a `rejection_reason STRING` column
- Quarantine is its own container in ADLS (`quarantine/`) for separate lifecycle management

Rationale: rejects originate from bronze ingestion as well as silver expectation failures,
so burying quarantine inside `silver.*` doesn't fit cleanly.

---

## 6. What's pending in Module 3

- [ ] Phase A Stage 5 — smoke test write into `retaildp.bronze`
- [ ] Phase B Stage 6 — create compute cluster (Single User, 13.3 LTS or later)
- [ ] Phase B Stage 7 — connect Databricks Repos to `satyakiguha007/retail-data-platform`
- [ ] Bronze notebooks (5 files in `transformations/bronze/`)
- [ ] Silver notebooks (8 files in `transformations/silver/`)
- [ ] Gold notebooks (4 dims + 2 facts in `transformations/gold/`)

See `docs/module3_progress.md` for the granular checklist.
