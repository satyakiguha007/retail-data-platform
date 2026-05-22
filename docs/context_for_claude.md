# Current project state

Last updated: 2026-05-21

## Done

### Module 1 — POS RTLOG Simulator (`pos_simulator/`)
- 26 stores across India/USA/UK/UAE/Singapore (stores.csv)
- All 8 TRAN_TYPEs, 6 fault types, IGTAX/TAX/BOTH modes
- NDJSON partitioned `store=/date=/hour=` (Auto Loader ready)
- CLI, Dockerfile, 41 passing tests, full docs

### Module 2 — Batch Sources & Additional Channels (`ingestion/`)
- **FX rates** — synthetic GBM + ECB API, 5 currencies, MERGE on (date × currency)
- **Marketplace simulator** — 7 marketplaces, 20 SKUs, partitioned NDJSON, `RTLOG_ORIG_SYS='MKT'`
- **Olist Bronze** — 9 CSVs with enforced StructType schemas
- **Weather** — synthetic seasonal + Open-Meteo ERA5 API, 26 stores
- **Shared UI** — Tkinter popups for interactive source selection

### Module 2b — ADLS Sync Console (`adls_sync_console/`)
- Streamlit app: connection page + adhoc upload + scheduler + statistics
- ThreadPoolExecutor (8 workers), validation rules per source, 500-run history
- Uploads via service principal in `.env`

### Module 3 Phase A — Storage + Unity Catalog ✅ CLOSED
- 8 ADLS containers, 7 external locations, catalog `retaildp` + 4 schemas with managed locations
- Smoke test passed, physical files in bronze container confirmed
- Convention: quarantine is `retaildp.quarantine` schema (not `silver._quarantine`)

### Module 3 Phase B — Compute + Repos ✅ CLOSED
- Serverless compute model — SQL Warehouse + serverless notebook compute
- GitHub PAT linked, Git folder cloned at `/Workspace/Users/satyakiguha007@gmail.com/retail-data-platform/`
- Public repo clones anonymously; PAT only needed for push

### Module 3 Bronze layer ✅ COMPLETE 2026-05-21
All 5 Bronze notebooks deployed under `transformations/bronze/`:

| Notebook | Pattern | Table |
|---|---|---|
| `01_fx_rates.py` | Batch CSV + MERGE on (date, currency pair) | `retaildp.bronze.fx_rates` |
| `02_weather.py` | Batch CSV + MERGE on (date, store) | `retaildp.bronze.weather` |
| `05_olist.py` | 9 CSVs in a config-driven loop + OVERWRITE | `retaildp.bronze.olist_*` (9 tables) |
| `03_pos_rtlog.py` | Auto Loader (`cloudFiles`) + `trigger(availableNow=True)` | `retaildp.bronze.pos_rtlog` |
| `04_marketplace.py` | Auto Loader (`cloudFiles`) + `trigger(availableNow=True)` | `retaildp.bronze.marketplace` |

All notebooks: strict StructType (or inferred + evolved for Auto Loader), `_ingest_ts` + `_source_file` metadata via `col("_metadata.file_path")`, Delta auto-optimize enabled, idempotent re-runs verified.

### UC Serverless gotchas hit and documented
- `input_file_name()` blocked → use `col("_metadata.file_path")`
- Mixed-type pandas display fails Arrow conversion → cast to `str` or use consistent types upstream
- `CLUSTER BY` fails when partition cols beyond stats schema (deep nested STRUCTs) → skip clustering at Bronze, defer to Silver

Full details in `docs/azure_databricks_state.md` §4.2 and `docs/bronze_layer_study_guide.md` Part 7.

---

## In progress

Nothing actively in progress — Bronze layer closed. Ready to start Silver (ReSA `SA_TRAN_*` canonical model).

---

## Next tasks (in order)

1. **Silver `01_sa_tran_head.py`** — explode POS RTLOG + marketplace into the canonical header table. Channel-conformed via `rtlog_orig_sys` discriminator. FX-normalized monetary columns.
2. **Silver `02_sa_tran_item.py`** — explode the `items` array, link to `tran_head` via composite key, FX-normalize prices.
3. **Silver `03_sa_tran_disc.py`, `04_sa_tran_tender.py`, `05_sa_tran_tax.py`, `06_sa_tran_igtax.py`** — same pattern, explode their respective nested arrays.
4. **Silver `07_sa_store_day.py`, `08_sa_store_data.py`** — store-level aggregates and metadata.
5. **Quarantine wiring** — every Silver notebook routes rejects to `retaildp.quarantine.silver_<table>_rejects` with `rejection_reason`.

After Silver: Gold (`dim_*`, `fact_*`).

---

## Bootstrap reading for Claude at session start

In priority order:

1. `CLAUDE.md` — project conventions, module status, reference doc index
2. `docs/context_for_claude.md` — this file (current state)
3. `docs/azure_databricks_state.md` — live cloud configuration (containers, UC, paths, compute model, gotchas)
4. `docs/module3_progress.md` — granular Module 3 checklist
5. `docs/bronze_layer_study_guide.md` — Bronze patterns reference (newest)
6. `docs/folder_structure.md` — repo + ADLS folder layout
7. `docs/resa_reference.md` — SA_* column specs (load at start of Silver work)

The first four are short and should always be loaded. Study guide and ReSA reference load when relevant to the current task.