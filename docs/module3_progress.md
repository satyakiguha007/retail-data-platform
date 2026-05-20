# Module 3 — Medallion Lakehouse Progress

**Folder:** `transformations/`
**Status:** Phase A (storage + catalog) nearly complete; Phase B + Bronze notebooks pending
**Last updated:** 2026-05-20

---

## Phase A — Storage + Catalog

| Stage | What | Status |
|---|---|---|
| 1 | Verify storage credential and `ext_raw` external location | ✅ Done |
| 2 | Create bronze, silver, gold containers in ADLS (+ checkpoints, quarantine, artifacts added beyond original plan) | ✅ Done |
| 3 | Create external locations: `ext_bronze`, `ext_silver`, `ext_gold`, `ext_quarantine`, `ext_checkpoints`, `ext_artifacts` | ✅ Done |
| 4 | Create catalog `retaildp` + schemas `bronze`, `silver`, `gold`, `quarantine` with managed locations | ✅ Done |
| 5 | Smoke test — write a table into `retaildp.bronze`, verify physical path lands in `bronze` container | ⏸ **Next** |

### Stage 5 smoke test (when you return)

```sql
USE CATALOG retaildp;

CREATE TABLE bronze._smoke_test AS
SELECT 1 AS x, current_timestamp() AS created_at;

DESCRIBE EXTENDED bronze._smoke_test;
-- Confirm `Location` starts with abfss://bronze@stretaildpsatyaki01...

SELECT * FROM bronze._smoke_test;

DROP TABLE bronze._smoke_test;
```

If the location resolves correctly and the SELECT returns one row, Phase A is closed.

---

## Phase B — Compute + Repos

| Stage | What | Status |
|---|---|---|
| 6 | Create cluster (Single User, 13.3 LTS or later, smallest worker, autoterminate 30 min) | ⏸ Pending |
| 7 | Generate GitHub PAT, connect Databricks Repos to `satyakiguha007/retail-data-platform`, clone | ⏸ Pending |
| 8 | Verify `/Workspace/Repos/.../transformations/bronze/` visible and empty | ⏸ Pending |

---

## Bronze notebooks (5)

Build order — simplest first, so the Auto Loader pattern is learned on easy data before
the nested POS RTLOG schema is tackled.

| Order | Notebook | Pattern | Status |
|---|---|---|---|
| 1 | `01_fx_rates.py` | Flat CSV → Delta, MERGE on (date, currency) | ⏸ Drafted in last session, not yet placed |
| 2 | `02_weather.py` | Clone-and-modify of `01` — different keys (date, store) | ⏸ Pending |
| 3 | `05_olist.py` | 9 static CSVs in a loop, enforced StructType schemas, overwrite (not MERGE) | ⏸ Pending |
| 4 | `03_pos_rtlog.py` | Auto Loader, nested JSON, partition discovery on `store/date/hour` | ⏸ Pending |
| 5 | `04_marketplace.py` | Auto Loader, flatter NDJSON, partition on `marketplace/date` | ⏸ Pending |

After all five are running, Bronze is closed and Silver work begins.

---

## Silver notebooks (8) — Not started

ReSA SA_* canonical, one notebook per table. All channels (POS / MKT / OMS) land in the
same Silver tables, differentiated by `RTLOG_ORIG_SYS`.

- [ ] `01_sa_tran_head.py`
- [ ] `02_sa_tran_item.py`
- [ ] `03_sa_tran_disc.py`
- [ ] `04_sa_tran_tender.py`
- [ ] `05_sa_tran_tax.py`
- [ ] `06_sa_tran_igtax.py`
- [ ] `07_sa_store_day.py`
- [ ] `08_sa_store_data.py`

Monetary columns join to `retaildp.bronze.fx_rates` and produce a `*_usd` column in addition
to the original currency value.

Rejected rows land in `retaildp.quarantine.silver_<table>_rejects` with a `rejection_reason`.

---

## Gold notebooks — Not started

- [ ] `dim_store.py`, `dim_product.py`, `dim_date.py`, `dim_customer.py`
- [ ] `fact_sales_line.py`
- [ ] `fact_audit_error.py` (built later, after Module 4 audit engine)

---

## Reference

- Live cloud state: `docs/azure_databricks_state.md`
- ReSA column specs: `docs/resa_reference.md`
- Architecture + design rationale: `docs/retail_data_platform_design_v1.1.pdf`
- Repo + ADLS folder structure: `docs/folder_structure.md`
