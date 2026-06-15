# Conventions

Code patterns, naming rules, and `_shared/` library APIs across the project.
This file is additive — each section names the convention, why it exists, and
where to read the canonical implementation.

---
Olist's silver discriminator is RTLOG_ORIG_SYS='OMS', not OLIST. Any gold join to the Olist channel keys on OMS


## Silver layer

### Surrogate keys (xxhash64)

- `TRAN_SEQ_NO = xxhash64(RTLOG_ORIG_SYS, TRAN_SEQ_NO_NATURAL, TRAN_DATETIME)`
- `STORE_DAY_SEQ_NO = xxhash64(STORE, BUSINESS_DATE)`
- `ERROR_SEQ_NO = xxhash64(TRAN_SEQ_NO, RULE_ID)` *(audit layer)*

`TRAN_DATETIME` must be cast to `TimestampType()` **before** hashing. Module 1
fault injection makes both the natural composite and the source `tran_seq_no`
non-unique — `tran_datetime` is the tie-breaker. Canonical implementation:
`_shared/surrogate_keys.py`.

### FX inheritance

Children call `enrich_with_parent_fx(keyed, PARENT_TABLE, [join_keys])`. Inherits
`CURRENCY_CODE` + `FX_RATE` from the parent silver table. `sa_tran_head` is the
root and derives FX from `bronze.fx_rates` directly. Never re-derive FX in
children. Canonical: `_shared/fx_helpers.py`.

### Quarantine pattern

DQ failures route to `retaildp.quarantine.silver_<table>_rejects` with
`rejection_reason ArrayType<String>`. Helper `merge_and_quarantine()` handles
both the MERGE and the quarantine append idempotently. Canonical:
`_shared/quarantine.py`.

### Schema gate

Notebooks that explode optional bronze arrays (`tran_tax`, `tran_igtax`) check
`bronze_array_has_inner_fields(...)` before starting the stream. Lesson from
debugging IND-only Pass-1 data (Auto Loader infers empty arrays as no-field
structs). Canonical: `_shared/schema_gate.py`.

### Partitioning

Every silver Delta table partitioned by `BUSINESS_DATE`. No `DAY` column
anywhere. `TRAN_SEQ_NO` is the unique surrogate that replaces the
`(STORE, DAY, TRAN_SEQ_NO)` ReSA composite.

### Channel discriminator

Every silver row carries `RTLOG_ORIG_SYS` ∈ `{POS, MKT, OMS}`. Same silver
table holds all three channels. Surrogate key starts with `RTLOG_ORIG_SYS` so
cross-channel collisions are structurally impossible.

### Helper loading

`%run ../_shared/<name>` — relative `%run`, not `import`. Avoids `sys.path`
setup, works with digit-prefixed folder names. The `%run` cascade also means
transitive imports work: if `rule_framework` `%run`s `sa_error_schema`, then
a rule that `%run`s only `rule_framework` still sees `sa_error_schema`'s
top-level variables.

### Streaming pattern (POS / MKT)

`readStream + availableNow + foreachBatch` for streaming sources; **batch** for
static dims (Olist, FX, weather, stores). Inside `foreachBatch`:

1. Aggregate / flatten the microbatch
2. Conformance — rename, derive keys, lineage
3. FK validation via broadcast joins (small dims)
4. MERGE clean rows into target; append rejects to quarantine

### Idempotent MERGE on PK

All silver and audit writes use MERGE on the surrogate PK. Re-running on the
same source data produces the same PKs → MERGE updates existing rows, no
duplicates.

---

## Audit layer (Module 4)

### Rule notebook template

Every rule under `transformations/silver/audit/rules/` follows this shape:

```python
# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R0X — <one-line summary>
# MAGIC [property table, expected outcomes, etc.]

from pyspark.sql.functions import col, lit, concat, ...
from pyspark.sql.types import DecimalType, StringType

# MAGIC %run ../_shared/rule_framework

RULE_ID   = "R0X_<NAME>"
RULE_NAME = "<human-readable rule description>"
SEVERITY  = Severity.<FATAL|MINOR|WARNING>

# Pre-run cleanup — handles rule-logic upgrades
if spark.catalog.tableExists(TARGET_TABLE):
    spark.sql(f"DELETE FROM {TARGET_TABLE} WHERE RULE_ID = '{RULE_ID}'")

def run(spark) -> DataFrame:
    # 1. Read inputs
    # 2. Compute drift / violations
    # 3. Project to narrow_finding_schema (8 cols)
    return emit_findings(narrow, RULE_ID, RULE_NAME, SEVERITY)

# Standalone execution
findings = run(spark)
write_findings(findings)

# Validation — print counts, samples, by-channel breakdown
```

### Narrow finding contract

Each rule's `run()` returns a DataFrame with exactly these 8 columns:

| Column | Type | Notes |
|---|---|---|
| `TRAN_SEQ_NO` | LongType | FK → sa_tran_head (or a pseudo-key for non-tran-level rules like R01) |
| `STORE` | LongType | |
| `BUSINESS_DATE` | DateType | |
| `RTLOG_ORIG_SYS` | StringType | POS / MKT / OMS |
| `MEASURED_VALUE` | DecimalType(20,4) | what the rule observed (nullable) |
| `EXPECTED_VALUE` | DecimalType(20,4) | what the rule expected (nullable) |
| `DELTA` | DecimalType(20,4) | difference (nullable) |
| `ERROR_DESC` | StringType | human-readable with all relevant numbers |

`emit_findings()` adds the rest — ERROR_SEQ_NO (hashed), RULE_ID/RULE_NAME/SEVERITY
(literals), `_audit_ts` (current_timestamp), `_audit_run_id` (NULL — set by writer).

### Severity convention (ReSA)

- `W` = Warning (informational, auditor reviews)
- `M` = Minor (needs auditor action)
- `F` = Fatal (blocks downstream processing)

Canonical: `Severity` class in `rule_framework.py`.

### Tax tables are informational, NOT arithmetic

`sa_tran_igtax` / `sa_tran_tax` break out tax components OF `head.VALUE`. They
are NOT separate variables to add or subtract in any reconciliation. Locked
ReSA convention discovered through R02's 5-iteration cycle.

### Schema verification discipline

Before writing any rule that reads silver tables, **search project knowledge
for the actual column names**. Don't write from ReSA-canonical memory. R02
went through three column-name slips (`IGTAX_AMT` → `TOTAL_IGTAX_AMT`,
`DISC_VALUE` → `UNIT_DISCOUNT_AMT`, etc.) all caused by skipping this step.

### Orchestrator pattern

`sa_error_writer.py` uses `dbutils.notebook.run()` over `%run` for:
- Iterable list of rule paths (loop, not 18 cells)
- Per-rule timing capture
- Exception isolation (one failed rule doesn't sink the batch)

After all rules complete, the orchestrator runs a single SQL `UPDATE` to set
`_audit_run_id = BATCH_RUN_ID` for all rows where `_audit_ts >= start_time`.
This shares one batch ID without modifying any rule file.

---

## Naming

| Object | Pattern | Example |
|---|---|---|
| Catalog | always `retaildp` | |
| Schema | `bronze`, `silver`, `quarantine` | |
| Bronze table | `bronze.<source>` | `bronze.pos_rtlog` |
| Silver table | `silver.sa_<entity>` (ReSA-canonical) | `silver.sa_tran_head` |
| Quarantine table | `quarantine.silver_<table>_<src>_rejects` | `quarantine.silver_sa_tran_head_pos_rejects` |
| Audit findings | `silver.sa_error` | |
| Notebook | `<NN>_<entity>_<source>.py` | `01_sa_tran_head_pos.py` |
| Audit rule notebook | `<NN>_<short_name>.py` | `02_head_items_total.py` |
| Rule ID | `R<NN>_<SHOUTING_SNAKE_CASE>` | `R02_HEAD_ITEMS_TOTAL` |

---

## Cross-cutting

### Decimal precision

All monetary columns use `DecimalType(20, 4)`. Never `FloatType` or `DoubleType`
on financial data. Precision artefacts at this scale are bounded and avoided.

### Logical operators in WHERE

Use bitwise `&` / `|` for combining column conditions:

```python
.where((col("X") > 0) & (col("Y").isNotNull()))
```

Not Python `and` / `or` — those evaluate Columns to booleans incorrectly.

### `lit()` for cross-type arithmetic

For DecimalType subtraction etc., use explicit `lit(0).cast(DecimalType(20, 4))`
to avoid Python-int conversion drift.

### Never TRUNCATE a Delta source with a streaming reader

TRUNCATE writes a delete commit that breaks downstream streams
(`DELTA_SOURCE_IGNORE_DELETE`). Use DROP + recreate instead. Documented as
operational lesson during Module 3 Bronze.

---

## `_shared/` library API summary

| Helper | File | Used by |
|---|---|---|
| `tran_seq_no_expr()` | `surrogate_keys.py` | every silver tran_* notebook |
| `enrich_with_parent_fx(df, parent, keys)` | `fx_helpers.py` | every child silver table |
| `merge_and_quarantine(...)` | `quarantine.py` | every silver writer |
| `bronze_array_has_inner_fields(...)` | `schema_gate.py` | tran_tax, tran_igtax |
| `Severity.{WARNING, MINOR, FATAL}` | `audit/_shared/rule_framework.py` | every audit rule |
| `emit_findings(narrow, rule_id, name, sev)` | `audit/_shared/rule_framework.py` | every audit rule |
| `write_findings(df, target, run_id=None)` | `audit/_shared/rule_framework.py` | every audit rule + orchestrator |

---

## When picking up work in a new session

1. Read `CLAUDE.md`, `docs/context_for_claude.md`, `docs/progress.md`
2. Identify which module's folder you're working in
3. For schema-dependent work: search project knowledge for the target table's schema **before** writing code
4. For silver work: check this file's `_shared/` library API summary above
5. For audit work: check `docs/audit-layer.md` for rule patterns and the framework
6. Verify Decimal precision, channel discriminator, FX inheritance haven't drifted from above
7. Commit at session boundaries: `git add . && git commit && git push`

Bronze→Silver in this lakehouse projects a narrow ReSA subset, not the full spec. Always run DESCRIBE TABLE against the actual silver table before writing transformations against it



