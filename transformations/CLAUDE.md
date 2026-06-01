# CLAUDE.md — Retail Data Platform

This file gives Claude the context to pick up work on this project at any point.
Read this first; deeper detail lives in `docs/`.

---

## What this project is

A medallion lakehouse on Azure + Databricks that replicates Oracle Retail
(RMS / RPM / ReSA) on a modern data engineering stack. Portfolio piece by
**Satyaki**, an Oracle Retail consultant transitioning to Data Engineering.

The differentiator: deep retail-domain fidelity (ReSA-canonical schema,
RTLOG-style ingestion, audit-grade DQ) layered on production-grade data
engineering patterns (Auto Loader, Delta MERGE, structured streaming,
broadcast joins, idempotent writes, quarantine-first).

- **GitHub**: `satyakiguha007/retail-data-platform`
- **Unity Catalog**: `retaildp` (schemas: `bronze`, `silver`, `quarantine`)
- **Storage**: ADLS Gen2 (`stretaildpsatyaki01`)

## Tech stack

- **Cloud**: Azure (ADLS Gen2, Databricks Unity Catalog)
- **Languages**: Python, PySpark, Spark SQL
- **Patterns**: Spark Structured Streaming, Delta Lake, `availableNow` + `foreachBatch`
- **Surrogate keys**: `xxhash64` of natural composites
- **Ingestion**: Auto Loader for bronze; `readStream` + MERGE for silver
- **Streaming buffer (planned)**: Kafka (Confluent Cloud or Redpanda)
- **Source generators**: Module 1 POS RTLOG simulator (POS),
  marketplace generator (Pass-2 planned), Olist Kaggle dataset (Pass-3 planned)

## Locked conventions — do not drift

1. **Surrogate key formula** lives in `_shared/surrogate_keys.py`:
   ```
   TRAN_SEQ_NO = xxhash64(RTLOG_ORIG_SYS, TRAN_SEQ_NO_NATURAL, TRAN_DATETIME)
   ```
   `TRAN_DATETIME` must be cast to `TimestampType()` BEFORE hashing.
   The cast is part of the formula. Drift here breaks every FK across silver.

2. **FK inheritance pattern** — child notebooks call
   `enrich_with_parent_fx(keyed, PARENT_TABLE, [join_keys])`.
   Inherits `CURRENCY_CODE` + `FX_RATE` from the parent silver table.
   Never re-derive these in children — always inherit.

3. **Quarantine, don't drop** — DQ failures route to
   `retaildp.quarantine.silver_<table>_rejects` with a `rejection_reason`
   `ArrayType<String>` column. Helper `merge_and_quarantine` handles both
   the MERGE and the quarantine append idempotently.

4. **Partitioning** — every silver table partitioned by `BUSINESS_DATE`.
   No `DAY` column anywhere. `TRAN_SEQ_NO` is the unique surrogate that
   replaces the (STORE, DAY, TRAN_SEQ_NO) ReSA composite.

5. **Schema gate** — notebooks that explode optional bronze arrays
   (`tran_tax`, `tran_igtax`) check `bronze_array_has_inner_fields(...)`
   before starting the stream. Lesson from 05 debugging on IND-only data.

6. **Channel discriminator** — every silver row carries `RTLOG_ORIG_SYS`
   ∈ {`POS`, `MKT`, `OLIST`}. Same silver table holds all three channels.
   Surrogate key starts with `RTLOG_ORIG_SYS` so cross-channel collisions
   are structurally impossible.

7. **`%run` over `import`** — helpers loaded via
   `# MAGIC %run ../_shared/<name>` (relative to each notebook).
   Avoids `sys.path` setup and works regardless of digit-prefixed folder names.

## Repo structure (silver layer)

```
transformations/silver/
├── _shared/                              # Loaded via %run from each notebook
│   ├── surrogate_keys.py                 # tran_seq_no_expr()
│   ├── fx_helpers.py                     # enrich_with_parent_fx()
│   ├── quarantine.py                     # merge_and_quarantine()
│   └── schema_gate.py                    # bronze_array_has_inner_fields()
├── 01_sa_tran_head/sa_tran_head_pos.py
├── 02_sa_tran_item/sa_tran_item_pos.py
├── 03_sa_tran_disc/sa_tran_disc_pos.py
├── 04_sa_tran_tender/sa_tran_tender_pos.py
├── 05_sa_tran_tax/sa_tran_tax_pos.py
└── 06_sa_tran_igtax/sa_tran_igtax_pos.py
```

Folder per silver table, file per source. Pass-2 adds `marketplace.py`
siblings; Pass-3 adds `olist.py`. The numeric prefix preserves build order.

## Current state — Pass-1 complete

| Notebook | Target | Pass-1 row count | Status |
|---|---|---:|---|
| `01_sa_tran_head/sa_tran_head_pos.py` | `silver.sa_tran_head` | 8,268 | ✅ |
| `02_sa_tran_item/sa_tran_item_pos.py` | `silver.sa_tran_item` | 22,649 | ✅ |
| `03_sa_tran_disc/sa_tran_disc_pos.py` | `silver.sa_tran_disc` | 5,094 | ✅ |
| `04_sa_tran_tender/sa_tran_tender_pos.py` | `silver.sa_tran_tender` | confirmed | ✅ |
| `05_sa_tran_tax/sa_tran_tax_pos.py` | `silver.sa_tran_tax` | 0 (IND-only, gate skips) | ✅ |
| `06_sa_tran_igtax/sa_tran_igtax_pos.py` | `silver.sa_tran_igtax` | confirmed | ✅ |

All POS notebooks refactored onto the four `_shared/` helpers.
PK uniqueness, FK integrity, and `sa_tran_igtax` ↔ `sa_tran_item.TOTAL_IGTAX_AMT`
reconciliation all pass.

## What's next

1. **Pass-2 — marketplace** — start with `01_sa_tran_head/marketplace.py`.
   Same `_shared/` helpers, different bronze shape (e-commerce orders),
   same target silver tables (rows distinguished by `RTLOG_ORIG_SYS='MKT'`).
   Coverage: 01, 02, 04, plus maybe 03 / 06 depending on simulator output.
2. **Pass-3 — Olist** — Brazilian e-commerce Kaggle dataset.
   Narrower coverage: head / item / tender only (no discount or
   per-line tax tables).
3. **Gold layer** — not started. Aggregates and analytic models on top
   of silver (sales by store-day, customer LTV, channel comparisons).
4. **Module 4 — audit** — `sa_error` ingestion and reconciliation reports.
   Planned after Pass-3.
5. **Second portfolio project** — Microsoft Fabric supply chain intelligence
   (separate repo, leveraging DP-700 cert).

## Key things to remember

- **The 3 stores in Pass-1 are all IND** (33487, 39876, 41203).
  All `TAX_MODE = IGTAX`. USA / GBR / ARE / SGP stores exist in
  `stores.csv` but weren't selected for the Pass-1 smoke test.
  This is why `sa_tran_tax` is empty (correct, not a bug).

- **Module 1 simulator's fault injection** makes BOTH the natural
  composite `(store, date, register, tran_no)` AND the simulator's
  own `tran_seq_no` non-unique. `tran_datetime` is the tie-breaker
  that makes `TRAN_SEQ_NO` truly unique. This is why the surrogate
  formula includes `TRAN_DATETIME` cast to `TimestampType()`.

- **Auto Loader schema inference quirk** — if a nested array
  (`tran_tax`, `tran_igtax`) was always empty during bronze ingestion,
  its inner struct has no fields. References like `col("tax.tax_seq_no")`
  then fail at plan time. Schema gate guards against this.

- **Foreign tenders are not exercised in Pass-1** — `ORIG_CURRENCY`
  always equals `CURRENCY_CODE`. The schema supports drift; Pass-2's
  cross-border marketplace orders will exercise it.

## Cross-machine workflow

- **End of session**: `git add . && git commit && git push`
- **Start of session**: `git pull`
- **Environment**: Windows Command Prompt or PowerShell — no WSL needed
- **Editor**: VS Code or Databricks notebook UI

## Working style preferences

- **Compressed timelines** — 7 years of QA / Oracle Retail context
  means no beginner scaffolding needed
- **Real-world complexity** — production-grade patterns valued over
  toy examples
- **Call-trace style** for code walkthroughs — concrete inputs, count
  the calls, show the tree, pause before going deeper
- **Domain knowledge as differentiator** — Oracle Retail / ReSA expertise
  is intentionally surfaced in design choices (table naming, ingestion
  flow, audit patterns)

## Reference materials (treat as pre-loaded)

- RMS 16.0 data model (bronze dimension references)
- ReSA 16.0 Operations Guide (silver schema authority — file in `/mnt/project/`)
- RPM 16.0 data model (promotions / price changes)

## Pointers to deeper docs

- `docs/silver-layer.md` — the medallion silver narrative, table by table
- `docs/progress.md` — pass-by-pass progress tracker
- `docs/conventions.md` — code conventions and the `_shared/` library API
