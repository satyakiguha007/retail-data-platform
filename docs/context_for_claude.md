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

### Module 3 Phase A — Storage + Unity Catalog (DONE 2026-05-20)
- 8 ADLS containers provisioned: `raw`, `bronze`, `silver`, `gold`, `quarantine`, `checkpoints`, `artifacts` (+ `$logs`)
- Access connector granted Storage Blob Data Contributor (storage-account scope)
- 7 external locations: `ext_raw`, `ext_bronze`, `ext_silver`, `ext_gold`, `ext_quarantine`, `ext_checkpoints`, `ext_artifacts`
- Catalog `retaildp` created with 4 schemas (`bronze`, `silver`, `gold`, `quarantine`), each with managed location pointing to its container

| 3 | Medallion Lakehouse (Bronze → Silver ReSA → Gold) | `transformations/` | **Phase A in progress** (storage + UC done; smoke test pending) |
Modules 1, 2, 2b complete. Module 3 Phase A storage + Unity Catalog setup done — final smoke test and Phase B (compute + Repos) pending.


**Convention change:** > 
- **Quarantine, don't drop** — records that fail DLT expectations or conformance checks go to
the `retaildp.quarantine` schema (source-prefixed table names, e.g. `pos_rejects`,
`silver_sa_tran_head_rejects`), each with a `rejection_reason` column. Never silently
discard bad rows. See `docs/azure_databricks_state.md` §5 for rationale.

- **Active catalog is `retaildp`** — all tables are created under `retaildp.{bronze,silver,gold,quarantine}`. The auto-created workspace catalog `dbw_retaildp_001` is unused. Every notebook should start with `USE CATALOG retaildp;`.



---

## In progress

Module 3 Phase A Stage 5 — smoke test write into `retaildp.bronze` to close out Phase A.

---

## Next tasks (in order)

1. **Phase A Stage 5** — Create and drop a test Delta table in `retaildp.bronze`, confirm physical files land in the `bronze` container. See `docs/module3_progress.md` for the SQL.

2. **Phase B Stage 6** — Provision a small Databricks cluster: Single User, Runtime 13.3 LTS (or later), smallest worker, autoterminate 30 min.

3. **Phase B Stage 7** — Generate GitHub PAT, link Databricks Repos to `satyakiguha007/retail-data-platform`, clone into workspace.

4. **First Bronze notebook** — `transformations/bronze/01_fx_rates.py`. The simplest pattern (flat CSV → Delta MERGE). A draft was prepared in an earlier session; place it in the Repos clone, run, commit, push.

5. **Remaining Bronze notebooks** — `02_weather`, `05_olist`, `03_pos_rtlog`, `04_marketplace` in that order (easy → hard).

---

## Bootstrap reading for Claude at session start

In priority order:

1. `CLAUDE.md` — project conventions, module status, reference doc index
2. `docs/context_for_claude.md` — this file (current state)
3. `docs/azure_databricks_state.md` — live cloud configuration (containers, UC, paths)
4. `docs/module3_progress.md` — granular Module 3 checklist
5. `docs/folder_structure.md` — repo + ADLS folder layout
6. `docs/resa_reference.md` — SA_* column specs (read when Silver work begins)



| Question | Go to |
|---|---|
| Live cloud state (Azure resources, UC catalog, external locations, paths) | `docs/azure_databricks_state.md` |
| Module 3 granular progress and Bronze/Silver/Gold checklist | `docs/module3_progress.md` |

The first four are short and should always be loaded. The other two are deep references —
load on demand when working on specific modules.
