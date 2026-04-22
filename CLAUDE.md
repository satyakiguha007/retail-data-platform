# Retail Data Platform — CLAUDE.md

## What this project is
A portfolio-grade, end-to-end retail data platform on Azure + Databricks. Three source channels
(in-store POS, e-commerce via Olist, simulated marketplace) are ingested through ADF + Auto Loader
into a medallion lakehouse. The Silver layer conforms all channels into the Oracle ReSA canonical
data model (11 core SA_* tables). A ReSA-style Sales Audit engine applies 18 reconciliation rules,
feeding a Kimball Gold layer, three LLM use cases (review enrichment, text-to-SQL, weekly narrative),
and Power BI dashboards. The project is built by Satyaki Guha, leveraging 7+ years of Oracle Retail
QA experience as a portfolio differentiator.

## Critical conventions
- **Y/N flags, not booleans** — all `*_IND` columns (e.g. `POS_TRAN_IND`, `ERROR_IND`,
  `DROP_SHIP_IND`) store `'Y'` / `'N'` as VARCHAR(1). Never use Python `True`/`False` or SQL
  `BOOLEAN`.
- **ReSA column-name fidelity** — every column name, type, and nullability in Silver must exactly
  match Oracle RMS 16.0. Do not rename, abbreviate, or add columns without an explicit decision.
  Source of truth: `docs/resa_reference.md`.
- **Partition on STORE + DAY** — all SA_TRAN_* and SA_STORE_DAY tables are partitioned by
  `STORE` + `DAY` (DAY is derived via SA_DATE_HASH, not a calendar date). Match this in every
  Delta table definition.
- **Quarantine, don't drop** — records that fail DLT expectations or conformance checks go to
  `silver._quarantine` with a `rejection_reason` column. Never silently discard bad rows.
- **RTLOG_ORIG_SYS is the channel key** — `'POS'` / `'OMS'` / `'MKT'`. All downstream queries
  that slice by channel filter on this column. Never add a separate channel field.
- **ReSA monetary columns use NUMERIC(20,4)** — map these to Spark `DECIMAL(20,4)`, never
  `FLOAT` or `DOUBLE`. Precision matters for tender reconciliation.

## Reference docs
| Question | Go to |
|---|---|
| Architecture, tech stack, module specs, design decisions | `docs/retail_data_platform_design_v1.1.pdf` |
| Authoritative column names, types, nullability for all 11 SA_* tables | `docs/resa_reference.md` |
| Audit rule catalog (18 rules, ERROR_CODEs, triggers) | Design doc §4.4 |
| Repo layout and CI/CD | Design doc §5 |

## Module structure and status
| # | Module | Folder | Status |
|---|---|---|---|
| 1 | POS RTLOG Simulator | `pos_simulator/` | Not started |
| 2 | Batch Sources & Additional Channels | `ingestion/` | Not started |
| 3 | Medallion Lakehouse (Bronze → Silver ReSA → Gold) | `transformations/` | Not started |
| 4 | Sales Audit Layer (18 rules → SA_ERROR) | `audit/` | Not started |
| 5 | LLM Intelligence Layer | `llm/` | Not started |
| 6 | Serving & Visualization (Power BI) | `serving/` | Not started |

Repo scaffolding is complete; no source code exists yet. Module 1 is next.

## Out of scope for v1
- Training custom ML models (we use pre-trained LLMs only)
- ReSA `_REV` tables except `SA_TRAN_HEAD_REV` (CDF demo only)
- Full ReSA rule engine tables (`SA_RULE_*`, `SA_REALM_*`, `SA_VOUCHER_*`)
- Streaming from Event Hub (v1 uses file-drop landing zone)

## When starting a task
1. Identify which module and folder the work belongs to.
2. For any Silver table column, verify name/type/nullability in `docs/resa_reference.md` before
   writing schema or test code.
3. Confirm flag columns use `'Y'`/`'N'` VARCHAR(1), not booleans.
4. Confirm monetary columns use `DECIMAL(20,4)`, not `FLOAT`/`DOUBLE`.
5. Confirm Delta tables on transaction data are partitioned by `STORE` + `DAY`.
6. Confirm bad-record handling routes to `silver._quarantine` with `rejection_reason`.
7. Check `docs/retail_data_platform_design_v1.1.pdf` Appendix A (Decisions Log) before
   introducing any architectural change — the decision may already be settled.
8. Each module should be independently demo-able when done; don't leave it in a broken state.
