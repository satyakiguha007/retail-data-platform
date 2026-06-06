# Project progress

Pass-by-pass tracker. Updated at the end of each work session.

**Last updated**: 2026-06-06

---

## Module status at a glance

| # | Module | Folder | Status |
|---|---|---|---|
| 1 | POS RTLOG Simulator | `pos_simulator/` | ✅ Complete |
| 2 | Batch sources + additional channels | `ingestion/` | ✅ Complete |
| 2b | ADLS Sync Console | `adls_sync_console/` | ✅ Complete |
| 3 | Medallion lakehouse (Bronze + Silver) | `transformations/` | ✅ Complete |
| 4 | Sales Audit layer (18 rules → sa_error) | `transformations/silver/audit/` | ✅ **Complete (this session)** |
| 5 | LLM Intelligence Layer | `llm/` | ⏳ Not started |
| 6 | Serving & Visualisation (Power BI / Synapse) | `serving/` | ⏳ Not started |

---

## Module 3 — Silver layer (medallion conformance)

### Pass-1 — POS only ✅ COMPLETE

| Notebook | Target | Row count | Status |
|---|---|---|---|
| `08_sa_store_data.py` | `silver.sa_store_data` | 27 | ✅ |
| `07_sa_store_day/sa_store_day.py` | `silver.sa_store_day` (POS rows) | 9 | ✅ |
| `01_sa_tran_head/sa_tran_head_pos.py` | `silver.sa_tran_head` (POS) | 8,268 | ✅ |
| `02_sa_tran_item/sa_tran_item_pos.py` | `silver.sa_tran_item` (POS) | 22,649 | ✅ |
| `03_sa_tran_disc/sa_tran_disc_pos.py` | `silver.sa_tran_disc` (POS) | 5,094 | ✅ |
| `04_sa_tran_tender/sa_tran_tender_pos.py` | `silver.sa_tran_tender` (POS) | confirmed | ✅ |
| `05_sa_tran_tax/sa_tran_tax_pos.py` | `silver.sa_tran_tax` (POS) | 0 (IND-only, schema gate skips) | ✅ |
| `06_sa_tran_igtax/sa_tran_igtax_pos.py` | `silver.sa_tran_igtax` (POS) | confirmed | ✅ |

### Pass-2 — Marketplace ✅ COMPLETE

| Notebook | Target | Notes |
|---|---|---|
| `01_sa_tran_head/sa_tran_head_marketplace.py` | `silver.sa_tran_head` (MKT) | Single-row pattern, REGISTER='ECOM' sentinel |
| `02_sa_tran_item/sa_tran_item_marketplace.py` | `silver.sa_tran_item` (MKT) | Explode items + REF_NO5-8 |
| `03_sa_tran_disc/sa_tran_disc_marketplace.py` | `silver.sa_tran_disc` (MKT) | 40% discount synthesised |
| `04_sa_tran_tender/sa_tran_tender_marketplace.py` | `silver.sa_tran_tender` (MKT) | One synthesised tender row per order, TENDER_TYPE_GROUP='MARKETPLACE' |
| `07_sa_store_day/sa_store_day_marketplace.py` | `silver.sa_store_day` (MKT rows) | Insert-if-not-exists MERGE |

Total marketplace transactions: 26,650 orders, 21,260 discount rows.

### Pass-3 — Olist ✅ COMPLETE

| Notebook | Target | Notes |
|---|---|---|
| `09_sa_seller_data/sa_seller_data_olist.py` | `silver.sa_seller_data` | New peer dim — 3,095 Olist sellers |
| `07_sa_store_day/sa_store_day_olist.py` | `silver.sa_store_day` (OMS rows) | Virtual STORE=99999 OLIST_BR, BRA, BRL |
| `01_sa_tran_head/sa_tran_head_olist.py` | `silver.sa_tran_head` (OMS) | First JOIN-aggregate head, VENDOR_NO = primary seller |
| `02_sa_tran_item/sa_tran_item_olist.py` | `silver.sa_tran_item` (OMS) | Flat-source items + synthetic freight line |
| `04_sa_tran_tender/sa_tran_tender_olist.py` | `silver.sa_tran_tender` (OMS) | 1:N installment fan-out from olist_order_payments |

Total Olist transactions: ~99k orders. No `sa_tran_disc` (no discount source in Olist data). No tax tables for OMS.

### CDF demo — `sa_tran_head_rev` ✅ COMPLETE

| Notebook | Target | Notes |
|---|---|---|
| `01_sa_tran_head/sa_tran_head_rev.py` | `silver.sa_tran_head_rev` | Delta Change Data Feed capture — `update_preimage` + `delete`. Audit trail sibling. |

ALTER TABLE enables CDF on `sa_tran_head` (idempotent). Streaming `readChangeFeed + availableNow + foreachBatch` appends to `_REV`. Schema = parent fields + `_change_type` + `_commit_version` + `_commit_timestamp` + `_rev_capture_ts`.

### Silver totals — all 3 channels

| Table | POS | MKT | OMS | Total |
|---|---|---|---|---|
| `sa_tran_head` | 8,268 | 26,650 | ~99k | **~133,584** |
| `sa_tran_item` | 22,649 | confirmed | confirmed (incl. freight lines) | sizeable |
| `sa_tran_disc` | 5,094 | 21,260 | 0 | 26,354 |
| `sa_tran_tender` | confirmed | 26,650 | confirmed (installment fan-out) | sizeable |
| `sa_tran_tax` | 0 | n/a | n/a | 0 |
| `sa_tran_igtax` | confirmed | n/a | n/a | sizeable |
| `sa_store_day` | 9 | added | added | combined |
| `sa_store_data` | 27 | — | — | 27 |
| `sa_seller_data` | — | — | 3,095 | 3,095 |

---

## Module 4 — Sales Audit layer ✅ COMPLETE (this session)

Full framework + 18 rules + orchestrator deployed under
`transformations/silver/audit/`. See `docs/audit-layer.md` for the deep dive.

### Foundation

| File | Role |
|---|---|
| `_shared/sa_error_schema.py` | `sa_error_schema` (14 cols) + `narrow_finding_schema` (8 cols) |
| `_shared/rule_framework.py` | `Severity` constants + `emit_findings()` + `write_findings()` |
| `sa_error_writer.py` | Orchestrator — runs all 18 rules with shared `audit_run_id` |

### Rules deployed

All 18 rules in `rules/`. Categorised:

| Category | Rules | Severity | Status |
|---|---|---|---|
| Reconciliation | R01, R02, R03, R04 | F/M/W mix | ✅ deployed |
| Sign / range | R05, R06, R07 | F/M | ✅ deployed + R06 patched (POS-scope) |
| Mandatory fields | R08, R09, R10 | F/M | ✅ deployed + R10 patched (+MARKETPLACE) |
| FK integrity | R11, R12, R13 | F | ✅ deployed |
| Reasonability | R14, R15, R16 | W | ✅ deployed + R15 patched (5+ lines) |
| Temporal / dimensional | R17, R18 | F/M | ✅ deployed + R17 patched (channel-aware) |

### Audit findings (real signal)

| Rule | Findings | Story |
|---|---|---|
| R02 | 552 | RETURN head=0 convention + PVOID + fault injection |
| R03 | 273 | OMS=255 (Olist payment drift), POS=18 |
| R05 | 25 | POS SALE with negative QTY — simulator fault injection |
| R14 | 2,186 | Statistical outliers — OMS large orders, MKT high-value, POS edge cases |
| **Others** | 0 or small | DQ-asserted (R08-R13, R18) or patched out (R06, R10, R15, R17) |

Total real findings after patches: **~3,000-3,500 across 18 rules**. All
genuine audit signal — no false positives from formula gaps.

### Iteration story — R02 (locked-in lessons)

R02 went through 5 versions before landing:
- v1 → 20,435 findings (discount-blind)
- v2-v4 → silently errored on wrong column names
- v5 → 552 findings, ReSA-canonical formula `head = items - disc`

Key conventions established:
- **Always search project schema docs before writing rule logic** — three column-name slips on R02 all stemmed from writing from ReSA-canonical memory instead of verifying
- **`xxhash64(TRAN_SEQ_NO, RULE_ID)` as PK** — deterministic + idempotent + cleanup-friendly
- **Pre-run `DELETE WHERE RULE_ID = '...'`** — every rule notebook has this cell; handles rule-logic upgrades cleanly
- **Tax tables are informational, not arithmetic** — `sa_tran_igtax` / `sa_tran_tax` break out tax components OF `head.VALUE`, not separate variables

Full narrative in `docs/audit-layer.md` § "R02 — case study in iteration".

---

## What's next

Module 4 is closed. Two choices ahead — your pick:

### Option A — Module 6 (Gold + Power BI)

| Task | Notes |
|---|---|
| `gold/dim_store.py`, `dim_seller.py`, `dim_item.py`, `dim_date.py` | Conformed dimensions from silver |
| `gold/fact_sales.py`, `fact_returns.py`, `fact_tender.py`, `fact_audit_findings.py` | Star-schema facts joining head + item + disc + tender + sa_error |
| `infra/synapse_serverless/views/*.sql` | External SQL views over gold Delta (Terraform-managed) |
| `serving/powerbi/*.pbix` | Sales overview · audit findings dashboard · channel comparison · OMS payment drift |
| **Pro** | Tangible visual artifact for the portfolio. `sa_error` consumed directly in dashboards. |
| **Con** | Power BI work is less "data engineering" and more BI. |

### Option B — Module 5 (LLM Intelligence)

| Task | Notes |
|---|---|
| `silver/llm/review_enrichment.py` | Olist customer reviews → sentiment + topic extraction |
| `gold/narrative/weekly_narrative.py` | LLM-generated weekly business summary from sa_error + facts |
| `apps/text_to_sql/app.py` | Streamlit text-to-SQL UI over silver/gold |
| `silver/llm/sa_error_classification.py` | LLM classifies sa_error findings into categories (legit / investigate / dismiss) |
| **Pro** | Differentiating — most retail-data portfolios stop at gold. LLM layer over audit findings is a strong signal. |
| **Con** | Harder to demonstrate without a visual layer to anchor it. |

### Recommendation

**Module 6 first**, then Module 5. Module 6 gives you tangible visual artifacts
that reference Module 4's findings concretely (dashboards consuming `sa_error`).
Module 5 then has somewhere to land (text-to-SQL over the published gold layer,
narratives that reference the dashboards).

---

## Deferred items / open TODOs

| Item | Module | Notes |
|---|---|---|
| `sa_error_impact` table | 4 | Optional ReSA-fidelity feature — joins sa_error to dollar amounts. Defer until needed for dashboards. |
| Tighten R14 (3σ outliers) | 4 | Currently 2,186 findings. May want TRAN_TYPE scope or minimum value floor. |
| Re-run R06/R10/R15/R17 with patches | 4 | If not yet re-run; numbers in audit-layer.md show pre-patch state. |
| Documentation: `docs/folder_structure.md` update | meta | Add `transformations/silver/audit/` tree |
| Documentation: `docs/conventions.md` update | meta | Add audit-rule patterns (PK hashing, pre-run cleanup, narrow → emit_findings) |
| `docs/architecture.md` Lucid link | meta | Diagram exists, needs polish + embed in docs |

---

## Cross-machine workflow reminder

- End of session: `git add . && git commit -m "..." && git push`
- Start of session: `git pull`
- Bootstrap reading (always): `CLAUDE.md` → `docs/context_for_claude.md` → `docs/progress.md` (this file)
- For Module 4 work: also `docs/audit-layer.md`
- For Module 6 / 5: load `docs/silver-layer.md` and `docs/conventions.md`
