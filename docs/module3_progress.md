# Module 3 — Medallion Lakehouse Progress

**Folder:** `transformations/`
**Status:** Phase A + B + Bronze layer all complete; Silver layer next
**Last updated:** 2026-05-21

---

## Phase A — Storage + Catalog ✅ COMPLETE

| Stage | What | Status |
|---|---|---|
| 1 | Verify storage credential and `ext_raw` external location | ✅ Done |
| 2 | Create bronze, silver, gold + checkpoints, quarantine, artifacts containers | ✅ Done |
| 3 | Create external locations for all 7 containers | ✅ Done |
| 4 | Create catalog `retaildp` + schemas with managed locations | ✅ Done |
| 5 | Smoke test — wrote table to `retaildp.bronze`, verified files in `bronze` container | ✅ Done |

---

## Phase B — Compute + Repos ✅ COMPLETE

| Stage | What | Status |
|---|---|---|
| 6 | Serverless notebook compute verified | ✅ Done |
| 7 | GitHub PAT linked, Git folder cloned, test notebook committed and pushed | ✅ Done |
| 8 | `transformations/bronze/` visible in workspace | ✅ Done |

### Compute model
- **SQL Warehouse** (Serverless Starter Warehouse) for SQL Editor
- **Serverless Notebook Compute** for all PySpark notebooks
- No classic clusters

### GitHub auth nuance
Public repo clones anonymously; PAT only needed for push.

---

## Bronze notebooks (5) ✅ COMPLETE

| Order | Notebook | Pattern | Status |
|---|---|---|---|
| 1 | `01_fx_rates.py` | Batch CSV → Delta, MERGE on (rate_date, from_currency, to_currency) | ✅ Done |
| 2 | `02_weather.py` | Batch CSV → Delta, MERGE on (obs_date, store_no) | ✅ Done |
| 3 | `05_olist.py` | 9 static CSVs in config-driven loop, PERMISSIVE mode, OVERWRITE | ✅ Done |
| 4 | `03_pos_rtlog.py` | Auto Loader (`cloudFiles`), nested NDJSON, `trigger(availableNow=True)` | ✅ Done |
| 5 | `04_marketplace.py` | Auto Loader (`cloudFiles`), flatter NDJSON, `trigger(availableNow=True)` | ✅ Done |

### Bronze patterns established

Three write patterns, all idempotent:
1. **Batch + MERGE** — FX, Weather. Natural-key UPSERT for sources with possible revisions.
2. **Batch + OVERWRITE** — Olist. Static historical data, atomic replacement.
3. **Auto Loader streaming** — POS, Marketplace. Checkpoint-based exactly-once on file arrivals.

All Bronze notebooks: strict StructType (or inferred + persisted for Auto Loader), `_ingest_ts` + `_source_file` lineage columns, `delta.autoOptimize` enabled.

See `docs/bronze_layer_study_guide.md` for full pattern documentation.

### UC Serverless gotchas resolved during Bronze build

| Gotcha | Resolution |
|---|---|
| `input_file_name()` blocked | Use `col("_metadata.file_path")` |
| Mixed-type pandas → Arrow on `display()` | Cast columns to consistent type before display |
| `CLUSTER BY` fails on tables with deeply nested STRUCTs | Skip clustering at Bronze, defer to Silver |

---

## Silver notebooks (8) — Next up

ReSA SA_* canonical, one notebook per table. All channels (POS / MKT) land in same Silver tables, differentiated by `rtlog_orig_sys`. Monetary columns join to `retaildp.bronze.fx_rates` to produce `*_usd` companions. Rejected rows land in `retaildp.quarantine.silver_<table>_rejects` with `rejection_reason`.

- [ ] `01_sa_tran_head.py` — Transaction headers
- [ ] `02_sa_tran_item.py` — Line items (explode `items` array)
- [ ] `03_sa_tran_disc.py` — Discounts (explode `discounts` array)
- [ ] `04_sa_tran_tender.py` — Tenders/payments (explode `tenders` array)
- [ ] `05_sa_tran_tax.py` — TAX mode rows (explode `taxes`)
- [ ] `06_sa_tran_igtax.py` — IGTAX mode rows (explode `igtax`)
- [ ] `07_sa_store_day.py` — Per-store-per-day rollups
- [ ] `08_sa_store_data.py` — Store master attributes

---

## Gold notebooks — Not started

- [ ] `dim_store.py`, `dim_product.py`, `dim_date.py`, `dim_customer.py`
- [ ] `fact_sales_line.py`
- [ ] `fact_audit_error.py` (after Module 4 audit engine)

---

## Reference

- Bronze patterns deep dive: `docs/bronze_layer_study_guide.md`
- Live cloud state: `docs/azure_databricks_state.md`
- ReSA column specs: `docs/resa_reference.md`
- Architecture + design rationale: `docs/retail_data_platform_design_v1.1.pdf`
- Repo + ADLS folder structure: `docs/folder_structure.md`