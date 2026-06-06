# Audit layer — Module 4

The ReSA-equivalent Sales Audit layer on the lakehouse. After silver conformance
asks *"is this row well-formed?"*, the audit layer asks *"is this row internally
consistent and reasonable given the surrounding data?"* Eighteen rules cover the
ReSA audit taxonomy. Findings land in `silver.sa_error`, ready for an auditor
(or downstream LLM enrichment) to review.

| Item | Value |
|---|---|
| **Target table** | `retaildp.silver.sa_error` |
| **Number of rules** | 18 |
| **Framework** | `_shared/rule_framework.py` + `_shared/sa_error_schema.py` |
| **Orchestrator** | `sa_error_writer.py` |
| **Execution model** | Batch (post-conformance), runs as a Databricks Job |

---

## Why a separate layer

Silver conformance enforces structural correctness — schemas, types, NOT NULLs,
FK existence, basic DQ. That happens at write time, with rejects going to
`retaildp.quarantine.silver_*_rejects`.

The audit layer is a different question: *given that every row is structurally
valid, do the numbers and relationships make business sense?* Examples R02
catches that conformance can't:

- A transaction whose `head.VALUE` doesn't reconcile against `SUM(items) − SUM(disc)`
- A return with the wrong sign on tender
- An Olist order whose payment total drifts > 1% from item total
- A statistical outlier 15σ above the store-day mean

These are not data quality bugs — they're business-rule violations. Auditors
need to see them; they shouldn't go into quarantine.

---

## Repo structure

```
transformations/silver/audit/
├── _shared/
│   ├── sa_error_schema.py        # Single source of truth — sa_error_schema + narrow_finding_schema
│   └── rule_framework.py         # Severity constants, emit_findings(), write_findings()
├── rules/                        # 18 rule notebooks
│   ├── 01_store_day_count_reconcile.py
│   ├── 02_head_items_total.py
│   ├── 03_head_tender_total.py
│   ├── 04_disc_reasonable.py
│   ├── 05_neg_qty_consistent.py
│   ├── 06_tender_sign_by_type.py
│   ├── 07_itm_unit_retail_positive.py
│   ├── 08_item_required.py
│   ├── 09_vendor_required_oms.py
│   ├── 10_tender_type_valid.py
│   ├── 11_head_store_day_fk.py
│   ├── 12_item_head_fk.py
│   ├── 13_tender_head_fk.py
│   ├── 14_value_3_sigma_outlier.py
│   ├── 15_single_line_dominates.py
│   ├── 16_oversized_basket.py
│   ├── 17_datetime_business_date_aligned.py
│   └── 18_currency_matches_store.py
└── sa_error_writer.py            # Orchestrator — runs all 18 with shared audit_run_id
```

---

## `SA_ERROR` schema

The canonical audit findings table. Partitioned by `BUSINESS_DATE`.

| Column | Type | Notes |
|---|---|---|
| `ERROR_SEQ_NO` | `LongType` | PK = `xxhash64(TRAN_SEQ_NO, RULE_ID)` — deterministic, idempotent on re-run |
| `TRAN_SEQ_NO` | `LongType` | FK → sa_tran_head |
| `STORE` | `LongType` | Denormalized for fast filtering / Power BI joins |
| `BUSINESS_DATE` | `DateType` | Partition key |
| `RTLOG_ORIG_SYS` | `StringType` | `'POS'` / `'MKT'` / `'OMS'` |
| `RULE_ID` | `StringType` | e.g. `'R02_HEAD_ITEMS_TOTAL'` |
| `RULE_NAME` | `StringType` | Human-readable summary |
| `SEVERITY` | `StringType` | `'W'` / `'M'` / `'F'` (ReSA convention — Warning / Minor / Fatal) |
| `MEASURED_VALUE` | `DecimalType(20, 4)` | What the rule observed (nullable) |
| `EXPECTED_VALUE` | `DecimalType(20, 4)` | What the rule expected (nullable) |
| `DELTA` | `DecimalType(20, 4)` | Difference (nullable) |
| `ERROR_DESC` | `StringType` | Human-readable explanation with all relevant numbers |
| `_audit_ts` | `TimestampType` | When the rule evaluated this row |
| `_audit_run_id` | `StringType` | Batch identifier — stamped by orchestrator |

### Idempotency

`ERROR_SEQ_NO = xxhash64(TRAN_SEQ_NO, RULE_ID)` is deterministic. Re-running a
rule on the same data produces the same PKs. MERGE on PK with
`whenMatchedUpdateAll` → updates existing findings, inserts new ones, no
duplicates.

If a previously-flagged transaction is fixed and re-conformed, the rule simply
won't fire on it; the stale finding remains in `sa_error` as a historical
record. Filter by latest `_audit_run_id` for "current open findings."

---

## Rule framework

Two design pieces in `_shared/`:

### `sa_error_schema.py`

Defines two `StructType`s:

- **`sa_error_schema`** — the full 14-column shape of the target Delta table
- **`narrow_finding_schema`** — the 8 columns each rule's `run()` MUST produce; the framework adds the rest

The split exists so rule authors focus on business logic (what to flag, with
what numbers) instead of repeating boilerplate for hashing / timestamps /
rule metadata.

### `rule_framework.py`

Three exports:

```python
class Severity:
    WARNING = "W"
    MINOR   = "M"
    FATAL   = "F"

def emit_findings(narrow_df, rule_id, rule_name, severity) -> DataFrame:
    """Stamps rule metadata + timestamps onto a narrow finding DF."""

def write_findings(findings, target_table=TARGET_TABLE, audit_run_id=None) -> int:
    """MERGE findings into sa_error. Bootstrap target on first write. Idempotent."""
```

### Rule pattern

Every rule notebook follows the same template:

```python
%run ../_shared/rule_framework

RULE_ID   = "R02_HEAD_ITEMS_TOTAL"
RULE_NAME = "..."
SEVERITY  = Severity.FATAL

# Pre-run cleanup — DELETE prior findings for this rule (handles rule-logic upgrades)
if spark.catalog.tableExists(TARGET_TABLE):
    spark.sql(f"DELETE FROM {TARGET_TABLE} WHERE RULE_ID = '{RULE_ID}'")

def run(spark) -> DataFrame:
    # ...compute drift...
    narrow = drift.select(TRAN_SEQ_NO, STORE, BUSINESS_DATE, RTLOG_ORIG_SYS,
                          MEASURED_VALUE, EXPECTED_VALUE, DELTA, ERROR_DESC)
    return emit_findings(narrow, RULE_ID, RULE_NAME, SEVERITY)

# Standalone execution
findings = run(spark)
write_findings(findings)

# Validation block — print stats, sample findings, etc.
```

Each rule is standalone-runnable for debugging. The orchestrator runs all 18 in
sequence (see below).

---

## The 18 rules

| # | Rule | Severity | Inputs | Scope | Status on Pass-3 data |
|---|---|---|---|---|---|
| **R01** | `STORE_DAY_COUNT_RECONCILE` | M | `sa_store_day`, `sa_tran_head` | (STORE, BUSINESS_DATE) | TBD |
| **R02** | `HEAD_ITEMS_TOTAL` — `head.VALUE = SUM(items) − SUM(disc)` | F | `head`, `item`, `disc` | per-tran | **552 findings** (real: PVOID/RETURN conventions + fault injection) |
| **R03** | `HEAD_TENDER_TOTAL` — `head ≈ SUM(tender)` within 1% | M | `head`, `tender` | per-tran | **273 findings** (real: OMS=255 payment drift, POS=18) |
| **R04** | `DISC_REASONABLE` — `abs(disc) ≤ abs(items)` | W | `head`, `item`, `disc` | per-tran | **0** (clean) |
| **R05** | `NEG_QTY_CONSISTENT` — neg QTY only on RETURN/CREFUND | F | `item`, `head` | per-tran | **25 findings** (real: POS fault injection) |
| **R06** | `TENDER_SIGN_BY_TYPE` — POS-only sign check | F | `head`, `tender` | per-tran POS | TBD post-patch |
| **R07** | `ITM_UNIT_RETAIL_POSITIVE` — ITM type → UNIT_RETAIL > 0 | M | `item` | per-tran | TBD |
| **R08** | `ITEM_REQUIRED` — `item.ITEM` non-null/non-blank | F | `item` | per-tran | 0 (DQ-asserted) |
| **R09** | `VENDOR_REQUIRED_OMS` — VENDOR_NO populated for OMS | M | `head` | per-tran | 0 (DQ-asserted) |
| **R10** | `TENDER_TYPE_VALID` — whitelist check | F | `tender` | per-tran | TBD post-patch |
| **R11** | `HEAD_STORE_DAY_FK` | F | `head`, `sa_store_day` | per-tran | 0 (DQ-asserted) |
| **R12** | `ITEM_HEAD_FK` | F | `item`, `head` | per-tran | 0 (DQ-asserted) |
| **R13** | `TENDER_HEAD_FK` | F | `tender`, `head` | per-tran | 0 (DQ-asserted) |
| **R14** | `VALUE_3_SIGMA_OUTLIER` — ±3σ of store-day mean | W | `head` (window over STORE, BUSINESS_DATE) | per-tran | **2,186 findings** (real: OMS large orders) |
| **R15** | `SINGLE_LINE_DOMINATES` — one line > 80% of tran value | W | `item` (window over TRAN_SEQ_NO) | per-tran | TBD post-patch |
| **R16** | `OVERSIZED_BASKET` — > 50 lines per tran | W | `item` | per-tran | TBD |
| **R17** | `DATETIME_BUSINESS_DATE_ALIGNED` — channel-aware tolerance | M | `head` | per-tran | TBD post-patch |
| **R18** | `CURRENCY_MATCHES_STORE` | F | `head`, `sa_store_data` | per-tran | 0 (FX inheritance enforced at conformance) |

### Category breakdown

| Category | Rules | Notes |
|---|---|---|
| Reconciliation (Totals) | R01, R02, R03, R04 | Balancing across tables |
| Sign / range integrity | R05, R06, R07 | Convention checks |
| Mandatory fields | R08, R09, R10 | NOT NULL + valid-set checks |
| FK integrity | R11, R12, R13 | Formal audit assertions over DQ-enforced FKs |
| Reasonability | R14, R15, R16 | Statistical / size anomaly detection |
| Temporal / dimensional | R17, R18 | Cross-dimension consistency |

---

## R02 — case study in iteration

R02 (`head.VALUE` reconciles against items and discounts) went through five
versions before landing on the right formula. Each iteration codified one ReSA
semantic that the previous version had baked in silently. Documented in full
because **the iteration itself is part of the portfolio story** — the audit
layer caught issues in the audit rule before any finding reached a human.

| Version | Formula | Findings | What it revealed |
|---|---|---|---|
| v1 | `head = SUM(items)` | 20,435 | Discount-blind. ~19,895 findings were just MKT/POS discounted transactions. |
| v2 | `head = SUM(items) − SUM(disc)` *(but with wrong column name `DISC_VALUE`)* | silently errored | Column was `UNIT_DISCOUNT_AMT`, not `DISC_VALUE`. Errored on read, but cleanup cell ran first → 0 findings in `sa_error` → looked like the rule "worked" but in fact never executed. |
| v3 | `head = items − disc **+** tax (with wrong column names)` | silently errored | Misread of statistical signal — `avg delta = 513.79 ≈ 18% of avg basket` looked like a GST-inclusive head hypothesis. It wasn't. |
| v4 | `head = items − disc **−** tax (sign flipped)` | silently errored | Still using wrong column names. |
| **v5 (final)** | `head = SUM(item.QTY × UNIT_RETAIL) − SUM(disc.QTY × UNIT_DISCOUNT_AMT)`, no tax adjustment | **552 findings** | ReSA convention: `sa_tran_igtax` / `sa_tran_tax` are informational breakdowns OF `head.VALUE`, not separate variables. Columns: `UNIT_DISCOUNT_AMT` (per-unit), multiply by `QTY` for line total. |

### Lessons that became locked conventions

1. **Read project schema docs before writing the rule, not after running it.**
   Three column-name slips on R02 (`IGTAX_AMT` → `TOTAL_IGTAX_AMT`,
   `DISC_VALUE` → `UNIT_DISCOUNT_AMT`, etc.) all came from writing
   ReSA-canonical names from memory instead of verifying. Working pattern
   now: search project docs first for every input table's schema.

2. **`xxhash64(TRAN_SEQ_NO, RULE_ID)` is idempotent, but stale findings persist
   when rule logic upgrades.** Each rule notebook needs a pre-run `DELETE WHERE
   RULE_ID = 'R0X_...'` cell. Without it, MERGE updates rows that fire under
   the new logic but leaves untouched rows that no longer fire — stale
   findings linger.

3. **Tax tables are informational, not arithmetic.** ReSA `sa_tran_igtax` /
   `sa_tran_tax` break out tax components OF `head.VALUE`. They're not
   separate variables you add or subtract to balance the equation.

### The 552 residual (genuine audit findings)

| TRAN_TYPE | head bucket | Count | Reason |
|---|---|---|---|
| RETURN | head=0 | 527 | POS simulator zeros `head.VALUE` for returns while keeping item lines (audit-trail convention) |
| SALE | head>0 | 22 | Fault-injected anomalies (POS simulator deliberately corrupts head value on ~0.3% of transactions) |
| SALE | head=0 | 3 | Edge cases — outlier fault injection |

All 552 are exactly what `sa_error` is meant to hold: an auditor should
investigate "transaction has items but no head value" or "sale flagged with
mismatched totals."

---

## Deferred refinements (now patched)

Four rules fired heavily on first run due to **channel convention or whitelist
gaps**, not real data issues. Each got a one-line patch after diagnostic.

| Rule | Original problem | Patch | Before → After |
|---|---|---|---|
| **R06** | Fired on MKT/OMS (3,670 findings). They use absolute tender values; direction is in `TRAN_TYPE`. | Scope to `RTLOG_ORIG_SYS == 'POS'` | 3,670 → small POS-only |
| **R10** | Fired on every MKT row (26,650 findings). `MARKETPLACE` tender type wasn't in whitelist. | Added `MARKETPLACE` to `VALID_TENDER_TYPE_GROUPS` | 26,650 → 0 |
| **R15** | Fired on every MKT order. Marketplace uses 2-line convention (main item + tiny fee), main always > 99% share. | Require `_line_count >= 5` before evaluating dominance | 6,387 → small |
| **R17** | Fired on 13,237 OMS rows. Olist payment-approval lag is 2-10 days — normal e-commerce behavior. | Channel-aware: `MAX_DIFF_BY_CHANNEL = {POS:1, MKT:1, OMS:14}` | 13,237 → tiny |

These patches are now in the rule files. Documented here because the iteration
story matters for understanding the data.

---

## Orchestrator — `sa_error_writer.py`

Single entry point for Module 4. Runs all 18 rules in dependency order via
`dbutils.notebook.run()`, captures per-rule timing + errors, then re-tags all
findings written during the batch with one shared `audit_run_id` via a single
SQL `UPDATE`.

### Design choices

| Choice | Why |
|---|---|
| `dbutils.notebook.run()` over `%run` | Iterable list, per-rule timing, exception isolation. One rule failing doesn't abort the batch. |
| Re-tag via SQL `UPDATE` after the fact | Avoids modifying any of the 18 rule files. Each rule writes with its own auto-generated run_id; orchestrator overwrites them all to `BATCH_RUN_ID` at the end. |
| `dbutils.notebook.exit(BATCH_RUN_ID)` | Job-friendly — downstream tasks (Power BI refresh, narrative generation) can read which batch ran. |

### Output

The orchestrator prints, at the end of each run:

- Total runtime + per-rule timings (sorted)
- OK / FAILED rule count
- Findings written this batch — by rule + severity, by channel, by severity × channel
- Distinct historical audit runs in `sa_error`

### Scheduling

Put `sa_error_writer.py` on a Databricks Job that depends on the silver
conformance Job. Each invocation = one complete audit pass with a fresh
`BATCH_RUN_ID`.

---

## Operational notes

| Note | Detail |
|---|---|
| **Rule re-runs are idempotent** | `ERROR_SEQ_NO = xxhash64(TRAN_SEQ_NO, RULE_ID)`. Same data + same rule = same PKs. |
| **Rule logic upgrades require cleanup** | Each rule notebook has a pre-run `DELETE WHERE RULE_ID = 'R0X_...'` cell. |
| **Severity = ReSA convention** | `W` = Warning (informational, auditor reviews), `M` = Minor (needs action), `F` = Fatal (blocks downstream). |
| **Findings persist across batches** | sa_error is append-with-update. Use `WHERE _audit_run_id = '<latest>'` for "current state." |
| **Quarantine vs sa_error** | Quarantine = DQ failures at write time (`silver_*_rejects`). sa_error = business-rule violations across-table. Different layers, different purposes. |

---

## What's next

| # | Task | Notes |
|---|---|---|
| 1 | Module 5 — LLM enrichment | Could consume sa_error directly: classify findings, generate auditor narratives, text-to-SQL UI |
| 2 | Module 6 — Gold layer + Power BI | `fact_audit_findings` joined to dim_store/dim_date for dashboards. Most natural visual artifact for the portfolio. |
| 3 | `sa_error_impact` table (v1.5) | Optional ReSA-fidelity feature — joins `sa_error` to dollar amounts. Defers to Module 6 if useful for dashboards. |
| 4 | Tighten R14 (3σ outliers) | Currently 2,186 findings. May want to scope by TRAN_TYPE or add minimum value floor. |
