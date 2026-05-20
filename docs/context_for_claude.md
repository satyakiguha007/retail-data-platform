# Current project state

Last updated: 2026-05-20

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

### Module 3 Phase A — Storage + Unity Catalog ✅ CLOSED 2026-05-20
- 8 ADLS containers provisioned: `raw`, `bronze`, `silver`, `gold`, `quarantine`, `checkpoints`, `artifacts` (+ `$logs`)
- Access connector granted Storage Blob Data Contributor (storage-account scope)
- 7 external locations: `ext_raw`, `ext_bronze`, `ext_silver`, `ext_gold`, `ext_quarantine`, `ext_checkpoints`, `ext_artifacts`
- Catalog `retaildp` created with 4 schemas (`bronze`, `silver`, `gold`, `quarantine`), each with managed location
- **Smoke test passed**: wrote table to `retaildp.bronze`, confirmed physical files in `bronze` container

**Convention change:** quarantine is a top-level schema (`retaildp.quarantine`), not `silver._quarantine`. See `docs/azure_databricks_state.md` §5.

### Module 3 Phase B Stage 6 — Compute ✅ Done 2026-05-20
- **Serverless notebook compute** verified: SQL + Python both read raw container via UC
- **Serverless SQL Warehouse** (auto-created Starter) used for SQL Editor
- **No classic clusters** — entire project runs on serverless

---

## In progress

Module 3 Phase B Stage 7 — Connect Databricks Git folder to `satyakiguha007/retail-data-platform` (GitHub PAT + Git folder add).

---

## Next tasks (in order)

1. **Stage 7** — Generate GitHub PAT, link in Databricks, add Git folder pointing at the repo. Verify clone lands at `/Workspace/Users/satyakiguha007@gmail.com/retail-data-platform/`.

2. **Stage 8** — Confirm `transformations/bronze/` is visible and empty in the workspace.

3. **First Bronze notebook** — `transformations/bronze/01_fx_rates.py`. Simplest pattern (flat CSV → Delta MERGE). A draft was prepared in an earlier session; place it in the Git folder clone, run on serverless, commit, push.

4. **Remaining Bronze notebooks** — `02_weather`, `05_olist`, `03_pos_rtlog`, `04_marketplace` in that order (easy → hard).

5. **Silver** then **Gold** (see `docs/module3_progress.md` for granular checklist).

---

## Bootstrap reading for Claude at session start

In priority order:

1. `CLAUDE.md` — project conventions, module status, reference doc index
2. `docs/context_for_claude.md` — this file (current state)
3. `docs/azure_databricks_state.md` — live cloud configuration (containers, UC, paths, compute model)
4. `docs/module3_progress.md` — granular Module 3 checklist
5. `docs/folder_structure.md` — repo + ADLS folder layout
6. `docs/resa_reference.md` — SA_* column specs (load when Silver work begins)

The first four are short and should always be loaded. The other two are deep references — load on demand.
