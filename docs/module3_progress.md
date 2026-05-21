# Module 3 — Medallion Lakehouse Progress

**Folder:** `transformations/`
**Status:** Phase A + B complete; first Bronze notebook done; building remaining Bronze
**Last updated:** 2026-05-20

---

## Phase A — Storage + Catalog ✅ COMPLETE

| Stage | What | Status |
|---|---|---|
| 1 | Verify storage credential and `ext_raw` external location | ✅ Done |
| 2 | Create bronze, silver, gold + checkpoints, quarantine, artifacts containers | ✅ Done |
| 3 | Create external locations for all 7 containers | ✅ Done |
| 4 | Create catalog `retaildp` + schemas with managed locations | ✅ Done |
| 5 | Smoke test — wrote table to `retaildp.bronze`, verified physical files in `bronze` container | ✅ Done |

---

## Phase B — Compute + Repos ✅ COMPLETE

| Stage | What | Status |
|---|---|---|
| 6 | Serverless notebook compute verified (SQL + Python read of raw works) | ✅ Done |
| 7 | GitHub PAT linked, Git folder cloned `retail-data-platform`, test notebook committed and pushed | ✅ Done |
| 8 | `transformations/bronze/` visible in workspace, ready for first notebook | ✅ Done |

### Compute model — important

This workspace uses **serverless compute exclusively**:

- **SQL Warehouse** (Serverless Starter Warehouse) → SQL Editor, dashboards
- **Serverless Notebook Compute** → All PySpark notebooks (Bronze/Silver/Gold)
- **No classic clusters** — don't reference cluster IDs or `spark.conf.set` cluster-level settings

Implications for code:
- Library installs use `%pip install <pkg>` at notebook scope (don't persist across sessions)
- `dbutils.fs` has some restrictions — prefer `spark.read` / `spark.write` paths
- `input_file_name()` is **blocked** — use `col("_metadata.file_path")` instead (see notebook 01 for working example)
- Auto Loader and Structured Streaming both work on serverless
- Per-second billing, no idle cost

### GitHub auth — useful nuance

Databricks Git folders authenticate **only on writes** (push). For a public repo, **clone and pull work anonymously**; the linked PAT is consulted only when you try to commit & push. If you set up the credential *after* cloning, the clone will succeed but the first push will fail until the PAT is linked. Either order works.

---

## Bronze notebooks (5)

Build order — simplest first.

| Order | Notebook | Pattern | Status |
|---|---|---|---|
| 1 | `01_fx_rates.py` | Flat CSV → Delta, MERGE on (date, currency) | ✅ Done |
| 2 | `02_weather.py` | Clone-and-modify of `01`; MERGE on (date, store_id) | ⏸ **Next** |
| 3 | `05_olist.py` | 9 static CSVs in a loop, enforced StructType, overwrite | ⏸ Pending |
| 4 | `03_pos_rtlog.py` | Auto Loader, nested JSON, partition discovery on `store/date/hour` | ⏸ Pending |
| 5 | `04_marketplace.py` | Auto Loader, flatter NDJSON | ⏸ Pending |

### Bronze template established (from `01_fx_rates.py`)

1. Config block — declare catalog/schema/table + raw path constants
2. Strict StructType — no schema inference
3. Read with `FAILFAST` mode
4. 4 quality gates (null check, value-range check, time-bound check, format check)
5. Add ingestion metadata: `_ingest_ts = current_timestamp()`, `_source_file = col("_metadata.file_path")`
6. `CREATE TABLE IF NOT EXISTS` with `delta.autoOptimize.optimizeWrite=true`
7. MERGE INTO on natural key
8. Summary by partition column
9. `DESCRIBE EXTENDED` to verify physical location

All four remaining Bronze notebooks follow this pattern with source-specific modifications.

---

## Silver notebooks (8) — Not started

ReSA SA_* canonical, one notebook per table. All channels (POS / MKT / OMS) land in same Silver tables, differentiated by `RTLOG_ORIG_SYS`.

- [ ] `01_sa_tran_head.py`
- [ ] `02_sa_tran_item.py`
- [ ] `03_sa_tran_disc.py`
- [ ] `04_sa_tran_tender.py`
- [ ] `05_sa_tran_tax.py`
- [ ] `06_sa_tran_igtax.py`
- [ ] `07_sa_store_day.py`
- [ ] `08_sa_store_data.py`

Monetary columns join to `retaildp.bronze.fx_rates` to produce `*_usd` companions.
Rejected rows land in `retaildp.quarantine.silver_<table>_rejects` with `rejection_reason`.

---

## Gold notebooks — Not started

- [ ] `dim_store.py`, `dim_product.py`, `dim_date.py`, `dim_customer.py`
- [ ] `fact_sales_line.py`
- [ ] `fact_audit_error.py` (after Module 4 audit engine)

---

## Reference

- Live cloud state: `docs/azure_databricks_state.md`
- ReSA column specs: `docs/resa_reference.md`
- Architecture + design rationale: `docs/retail_data_platform_design_v1.1.pdf`
- Repo + ADLS folder structure: `docs/folder_structure.md`