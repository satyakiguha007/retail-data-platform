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

### Module 3 Phase A — Storage + Unity Catalog ✅ CLOSED 2026-05-20
- 8 ADLS containers, 7 external locations, catalog `retaildp` + 4 schemas with managed locations
- Smoke test passed: wrote table to `retaildp.bronze`, files confirmed in bronze container
- Convention: quarantine is `retaildp.quarantine` schema (not `silver._quarantine`)

### Module 3 Phase B — Compute + Repos ✅ CLOSED 2026-05-20
- Serverless compute model verified — SQL Warehouse for SQL, serverless notebook compute for PySpark
- GitHub PAT linked, Git folder cloned at `/Workspace/Users/satyakiguha007@gmail.com/retail-data-platform/`
- Test notebook committed and pushed end-to-end (Databricks ↔ GitHub loop confirmed)
- Note: public repo clones anonymously; PAT only needed for push

### Module 3 Bronze — `01_fx_rates.py` ✅ Done 2026-05-20
- First Bronze notebook deployed at `transformations/bronze/01_fx_rates.py`
- Pattern: strict StructType → 4 quality gates → MERGE on (rate_date, from_currency, to_currency)
- Idempotent re-runs verified; physical files land in `bronze` container
- **UC serverless gotcha hit and resolved**: `input_file_name()` is blocked on UC serverless. Use `col("_metadata.file_path")` instead. This applies to ALL Bronze notebooks going forward.

---

## In progress

Module 3 Bronze — next notebook: `transformations/bronze/02_weather.py` (clone-and-modify of FX pattern; key is (rate_date, store_id) instead of (date, currency)).

---

## Next tasks (in order)

1. **`02_weather.py`** — same pattern as FX, MERGE on (date, store_id). 10-minute clone.
2. **`05_olist.py`** — 9 static CSVs in a loop with enforced StructType schemas, overwrite mode.
3. **`03_pos_rtlog.py`** — Auto Loader, nested JSON, partition discovery on `store/date/hour`.
4. **`04_marketplace.py`** — Auto Loader, flatter NDJSON.

After Bronze, Silver (8 ReSA tables) then Gold (4 dims + 2 facts).

---

## Bootstrap reading for Claude at session start

In priority order:

1. `CLAUDE.md` — project conventions, module status, reference doc index
2. `docs/context_for_claude.md` — this file (current state)
3. `docs/azure_databricks_state.md` — live cloud configuration (containers, UC, paths, compute model, gotchas)
4. `docs/module3_progress.md` — granular Module 3 checklist
5. `docs/folder_structure.md` — repo + ADLS folder layout
6. `docs/resa_reference.md` — SA_* column specs (load when Silver work begins)

The first four are short and should always be loaded. The other two are deep references — load on demand.