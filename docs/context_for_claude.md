# Current project state

Last updated: 2026-06-06

> **Where to start next session**: read `CLAUDE.md` → this file → `docs/progress.md`. If picking up Module 5/6, also load `docs/silver-layer.md` + `docs/audit-layer.md` + `docs/conventions.md`. If revisiting Module 4 (unlikely — it's complete), `docs/audit-layer.md` is the deep dive.

---

## Done

### Module 1 — POS RTLOG Simulator (`pos_simulator/`)
- 26 stores across India/USA/UK/UAE/Singapore (stores.csv updated to 27 with the virtual `99999 OLIST_BR`)
- All 8 TRAN_TYPEs, 6 fault types, IGTAX/TAX/BOTH modes
- NDJSON partitioned `store=/date=/hour=` (Auto Loader ready)
- CLI, Dockerfile, 41 passing tests, full docs

### Module 2 — Batch Sources & Additional Channels (`ingestion/`)
- **FX rates** — synthetic GBM + ECB API, 5 currencies, MERGE on (date × currency). BRL backfilled (Sep 2016-Oct 2018, linear interpolation) for Olist.
- **Marketplace simulator** — 7 marketplaces, 20 SKUs, partitioned NDJSON, `RTLOG_ORIG_SYS='MKT'`
- **Olist Bronze** — 9 CSVs with enforced StructType schemas
- **Weather** — synthetic seasonal + Open-Meteo ERA5 API, 26 stores

### Module 2b — ADLS Sync Console (`adls_sync_console/`)
Complete (no changes since prior).

### Module 3 — Medallion lakehouse ✅ COMPLETE

**Bronze**: 5 notebooks deployed under `transformations/bronze/`:
- `01_fx_rates.py`, `02_weather.py`, `03_pos_rtlog.py`, `04_marketplace.py`, `05_olist.py`

**Silver — all 3 channels**:
- **Pass-1 (POS)** — 8 notebooks: sa_store_data, sa_store_day, and 6 sa_tran_* tables. ~8,268 POS transactions across 3 IND stores.
- **Pass-2 (Marketplace)** — 5 notebooks extending sa_tran_head, item, disc, tender, store_day. ~26,650 MKT orders.
- **Pass-3 (Olist)** — 5 notebooks: sa_seller_data (new peer dim), sa_store_day, sa_tran_head, sa_tran_item (with synthetic freight lines), sa_tran_tender (with installment fan-out). ~99k Olist orders, virtual STORE=99999.
- **CDF demo** — `sa_tran_head_rev` captures `update_preimage` + `delete` revisions of `sa_tran_head` via Delta Change Data Feed.

Totals across silver: **~133,584 transactions** in sa_tran_head (POS + MKT + OMS).

### Module 4 — Sales Audit layer ✅ COMPLETE (this session)

Full ReSA-equivalent audit module under `transformations/silver/audit/`:

- **Framework**: `_shared/sa_error_schema.py` + `_shared/rule_framework.py` (Severity, emit_findings, write_findings)
- **18 rules** in `rules/` covering reconciliation, integrity, mandatory fields, FK, reasonability, temporal/dimensional
- **Orchestrator** `sa_error_writer.py` — runs all 18 via `dbutils.notebook.run()`, shared `BATCH_RUN_ID`, full summary
- **Target**: `retaildp.silver.sa_error` with partitioned-by-BUSINESS_DATE Delta

**Real audit findings on Pass-3 data**:
- R02: 552 (PVOID/RETURN conventions + fault injection)
- R03: 273 (OMS=255 Olist payment drift — main audit story)
- R05: 25 (POS fault-injected SALEs with negative QTY)
- R14: 2,186 (statistical outliers — OMS large orders)
- Others: 0 (DQ-asserted) or small (post-patch)

**Four rules patched post-initial-run** for channel-convention / whitelist gaps:
- R06 scoped to POS only (MKT/OMS use absolute tender values)
- R10 + `MARKETPLACE` in whitelist
- R15 requires `_line_count >= 5` (excludes MKT 2-line convention)
- R17 channel-aware tolerance `{POS:1, MKT:1, OMS:14}` (OMS payment-approval lag is normal)

See `docs/audit-layer.md` for the deep dive, especially the R02 5-iteration case study.

---

## In progress

Nothing actively in progress — Module 4 closed. Ready to pick **Module 5 (LLM)** or **Module 6 (Gold + Power BI)**.

---

## Next tasks (in suggested order)

**Recommendation: Module 6 first**, then Module 5. Module 6 gives tangible visual artifacts (Power BI dashboards consuming `sa_error`). Module 5 then has something concrete to enrich.

### Option A — Module 6 (Gold + Power BI)

1. **Gold dims**: `dim_store`, `dim_seller`, `dim_item`, `dim_date`
2. **Gold facts**: `fact_sales`, `fact_returns`, `fact_tender`, `fact_audit_findings`
3. **Synapse Serverless views** over gold Delta (Terraform-managed)
4. **Power BI dashboards**: sales overview, audit findings, channel comparison, OMS payment-drift drilldown

### Option B — Module 5 (LLM Intelligence)

1. **Review enrichment**: Olist customer reviews → sentiment + topics via Databricks Foundation Model APIs
2. **Text-to-SQL app**: Streamlit UI over silver/gold (swappable client architecture)
3. **Weekly narrative**: LLM-generated markdown summary from sa_error + facts
4. **sa_error classification**: LLM categorises findings (legit / investigate / dismiss)

---

## Locked engineering principles (do not drift)

Reaffirmed and added through Modules 3 + 4. Each one has a specific story behind it.

### Surrogate keys
- `TRAN_SEQ_NO = xxhash64(RTLOG_ORIG_SYS, TRAN_SEQ_NO_NATURAL, TRAN_DATETIME)` — TRAN_DATETIME cast to TimestampType BEFORE hashing
- `STORE_DAY_SEQ_NO = xxhash64(STORE, BUSINESS_DATE)`
- `ERROR_SEQ_NO = xxhash64(TRAN_SEQ_NO, RULE_ID)` (Module 4)

### Channel discriminator
- Every silver row carries `RTLOG_ORIG_SYS` ∈ {POS, MKT, OMS}
- Same silver table holds all three channels — surrogate key starts with RTLOG_ORIG_SYS so cross-channel collisions are structurally impossible

### Quarantine vs sa_error
- **Quarantine** = DQ failures at write time → `retaildp.quarantine.silver_*_rejects` with `rejection_reason ArrayType<String>`
- **sa_error** = business-rule violations across-table at audit time → `retaildp.silver.sa_error`
- Two distinct concerns, two distinct tables

### Partitioning
- Every silver Delta table partitioned by `BUSINESS_DATE`
- No `DAY` column anywhere
- `sa_error` also partitioned by `BUSINESS_DATE`

### FX inheritance
- Children call `enrich_with_parent_fx(keyed, PARENT_TABLE, [join_keys])` to inherit `CURRENCY_CODE` + `FX_RATE`
- `sa_tran_head` is the root — derives FX from `bronze.fx_rates` directly
- Never re-derive FX in child notebooks

### Pre-run cleanup for rule logic upgrades (Module 4)
- Each audit rule notebook has a pre-run cell: `DELETE FROM sa_error WHERE RULE_ID = '<id>'`
- Without it, MERGE updates rows that fire under new logic but leaves stale rows from old logic

### Tax tables are informational, not arithmetic (Module 4)
- `sa_tran_igtax` / `sa_tran_tax` break out tax components OF `head.VALUE`
- They're NOT separate variables to add or subtract in reconciliation
- This is the locked R02 lesson — ReSA convention

### Search project docs before writing schema-dependent code (Module 4)
- The R02 4-version detour was caused by writing column names from ReSA-canonical memory instead of project docs
- Working pattern now: search project knowledge for every input table's schema before writing rule/notebook logic

### Helper loading
- `%run ../_shared/<name>` — relative %run, not import
- Avoids sys.path setup, works with digit-prefixed folder names

---

## UC Serverless gotchas (carried forward)

- `input_file_name()` blocked → use `col("_metadata.file_path")`
- Mixed-type pandas display fails Arrow conversion → cast to `str` upstream
- `CLUSTER BY` fails when partition cols beyond stats schema (deep nested STRUCTs) → skip clustering at Bronze, defer to Silver
- `dbutils.notebook.run()` works on serverless (used by orchestrator)
- `%run` works on serverless (used throughout silver + audit)

---

## Bootstrap reading for Claude at session start

In priority order:

1. **Always**: `CLAUDE.md` — project conventions, module status, reference doc index
2. **Always**: `docs/context_for_claude.md` — this file (current state snapshot)
3. **Always**: `docs/progress.md` — pass-by-pass tracker
4. **Module-specific**:
   - If working on Module 4 (reopen): `docs/audit-layer.md`
   - If working on Module 5/6: `docs/silver-layer.md` + `docs/conventions.md`
5. **Lookup-on-demand**:
   - `docs/azure_databricks_state.md` — live cloud config
   - `docs/folder_structure.md` — repo + ADLS paths
   - `docs/resa_reference.md` — SA_* column specs (Oracle Retail authority)

---

## Open TODOs (carried into next session)

| Item | Module | Priority | Notes |
|---|---|---|---|
| `docs/folder_structure.md` — add `transformations/silver/audit/` tree | meta | low | Just a folder tree update |
| `docs/conventions.md` — document audit-rule patterns | meta | medium | PK hashing, pre-run cleanup, narrow → emit_findings, channel-aware logic |
| `docs/architecture.md` — embed Lucid link (or redraw) | meta | low | Existing Lucid is messy; could redo in Mermaid |
| `sa_error_impact` table | M4 | low | ReSA-fidelity, optional; defer until dashboards need it |
| Tighten R14 (3σ outliers) | M4 | low | 2,186 findings is a lot; may want TRAN_TYPE scope |
| Decide M5 vs M6 next | strategy | high | Recommend Module 6 first |
