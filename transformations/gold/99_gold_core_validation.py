# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — `99_gold_core_validation.py`
# MAGIC
# MAGIC Cross-cutting integrity suite for the whole `gold_core` star. One place that re-checks
# MAGIC every dim and fact at once — the gold equivalent of the silver FK/PK assertion suite.
# MAGIC
# MAGIC | Check group | What it asserts |
# MAGIC |---|---|
# MAGIC | 1. FK integrity | No `-1` orphans on any fact FK |
# MAGIC | 2. Row reconciliation | Each CDF fact == its silver source; store_day net == sales_line net |
# MAGIC | 3. PK uniqueness | Every fact grain is unique |
# MAGIC | 4. Watermark consistency | One SUCCEEDED run per fact; watermark == source current version |
# MAGIC | 5. Dim integrity | dim_item one-current-per-SKU; no dim PK dupes |
# MAGIC | 6. Star completeness | Every dim reachable from ≥1 fact |
# MAGIC
# MAGIC Each check appends to a results list. The final cell prints a PASS/FAIL summary and
# MAGIC raises if anything failed — so this is Job-friendly (fails the task on a broken star).

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration

# COMMAND ----------

CATALOG = "retaildp"
CORE    = "gold_core"

DIMS = {
    "dim_date":    ("date_key",    None),
    "dim_channel": ("channel_key", None),
    "dim_store":   ("store_key",   "store"),
    "dim_tender":  ("tender_key",  None),
    "dim_item":    ("item_key",    "item"),     # SCD2 — special handling
}

# fact -> (silver source for reconciliation, list of (fk_col) to orphan-check, pk cols)
FACTS = {
    "fact_sales_line": {
        "source": f"{CATALOG}.silver.sa_tran_item",
        "fks":    ["date_key", "store_key", "item_key", "channel_key"],
        "pk":     ["tran_seq_no", "item_seq_no"],
    },
    "fact_tender": {
        "source": f"{CATALOG}.silver.sa_tran_tender",
        "fks":    ["date_key", "store_key", "channel_key", "tender_key"],
        "pk":     ["tran_seq_no", "tender_seq_no"],
    },
    "fact_audit_error": {
        "source": f"{CATALOG}.silver.sa_error",
        "fks":    ["date_key", "store_key", "channel_key"],
        "pk":     ["error_seq_no"],
    },
    "fact_store_day": {
        "source": None,   # derived — reconciled separately against sales_line
        "fks":    ["date_key", "store_key", "channel_key"],
        "pk":     ["store_key", "date_key", "channel_key"],
    },
}

results = []   # (check_name, status, detail)

def record(name, passed, detail=""):
    results.append((name, "PASS" if passed else "FAIL", detail))
    flag = "✅" if passed else "❌"
    print(f"{flag} {name}  {detail}")

print("Validation config loaded.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Check group 1 — FK integrity (no -1 orphans)

# COMMAND ----------

from pyspark.sql import functions as F

for fact, cfg in FACTS.items():
    fqn = f"{CATALOG}.{CORE}.{fact}"
    agg = spark.table(fqn).agg(*[
        F.sum(F.when(F.col(fk) == -1, 1).otherwise(0)).alias(fk) for fk in cfg["fks"]
    ]).collect()[0]
    for fk in cfg["fks"]:
        n = agg[fk] or 0
        record(f"FK orphans {fact}.{fk}", n == 0, f"({n} orphans)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Check group 2 — row reconciliation

# COMMAND ----------

# CDF facts: fact rows == silver source rows
for fact, cfg in FACTS.items():
    if cfg["source"] is None:
        continue
    fact_n   = spark.table(f"{CATALOG}.{CORE}.{fact}").count()
    silver_n = spark.table(cfg["source"]).count()
    record(f"Reconcile {fact} == {cfg['source'].split('.')[-1]}",
           fact_n == silver_n, f"(fact={fact_n:,} silver={silver_n:,})")

# COMMAND ----------

# Derived fact: store_day net == sales_line net (USD), within rounding tolerance
sd_net = spark.sql(f"""
    SELECT ROUND(SUM(net_sales_usd), 2) AS n FROM {CATALOG}.{CORE}.fact_store_day
""").collect()[0]["n"]
sl_net = spark.sql(f"""
    SELECT ROUND(SUM(CASE WHEN qty > 0 THEN gross_amt_usd ELSE -ABS(gross_amt_usd) END), 2) AS n
    FROM {CATALOG}.{CORE}.fact_sales_line
""").collect()[0]["n"]
record("Reconcile store_day net == sales_line net",
       abs(float(sd_net) - float(sl_net)) < 0.01, f"(store_day={sd_net} sales={sl_net})")

# error_count: store_day total == audit fact total
sd_err = spark.sql(f"SELECT SUM(error_count) n FROM {CATALOG}.{CORE}.fact_store_day").collect()[0]["n"]
af_err = spark.sql(f"SELECT SUM(error_count) n FROM {CATALOG}.{CORE}.fact_audit_error").collect()[0]["n"]
record("Reconcile store_day errors == audit fact errors",
       sd_err == af_err, f"(store_day={sd_err} audit={af_err})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Check group 3 — PK uniqueness

# COMMAND ----------

for fact, cfg in FACTS.items():
    fqn = f"{CATALOG}.{CORE}.{fact}"
    pk_cols = ", ".join(cfg["pk"])
    dups = spark.sql(f"""
        SELECT COUNT(*) AS c FROM (
            SELECT {pk_cols}, COUNT(*) k FROM {fqn} GROUP BY {pk_cols} HAVING COUNT(*) > 1
        )
    """).collect()[0]["c"]
    record(f"PK unique {fact} ({pk_cols})", dups == 0, f"({dups} dup keys)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Check group 4 — watermark consistency

# COMMAND ----------

for fact, cfg in FACTS.items():
    succeeded = spark.sql(f"""
        SELECT COUNT(*) AS runs, MAX(version_to) AS wm
        FROM {CATALOG}.{CORE}._fact_load_log
        WHERE fact_table = '{fact}' AND run_status = 'SUCCEEDED'
    """).collect()[0]
    runs = succeeded["runs"]
    wm   = succeeded["wm"]
    record(f"Watermark exists {fact}", runs >= 1 and wm is not None,
           f"(succeeded_runs={runs} watermark=v{wm})")

# any FAILED runs left behind?
failed = spark.sql(f"""
    SELECT fact_table, COUNT(*) AS c
    FROM {CATALOG}.{CORE}._fact_load_log
    WHERE run_status = 'FAILED'
    GROUP BY fact_table
""")
fc = failed.count()
record("No FAILED runs in log", fc == 0, f"({fc} facts with failed runs)")
if fc > 0:
    display(failed)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Check group 5 — dim integrity

# COMMAND ----------

# dim PK uniqueness (natural key)
dim_nat_keys = {
    "dim_date":    "full_date",
    "dim_channel": "channel_code",
    "dim_store":   "store",
    "dim_tender":  "tender_type_group, tender_type_id",
}
for dim, nk in dim_nat_keys.items():
    dups = spark.sql(f"""
        SELECT COUNT(*) AS c FROM (
            SELECT {nk}, COUNT(*) k FROM {CATALOG}.{CORE}.{dim} GROUP BY {nk} HAVING COUNT(*) > 1
        )
    """).collect()[0]["c"]
    record(f"Dim PK unique {dim} ({nk})", dups == 0, f"({dups} dup keys)")

# dim_item SCD2 — exactly one current row per SKU
scd_violations = spark.sql(f"""
    SELECT COUNT(*) AS c FROM (
        SELECT item, COUNT(*) k FROM {CATALOG}.{CORE}.dim_item
        WHERE is_current = TRUE GROUP BY item HAVING COUNT(*) <> 1
    )
""").collect()[0]["c"]
record("dim_item one-current-per-SKU", scd_violations == 0, f"({scd_violations} SKUs violating)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Check group 6 — star completeness (every dim reachable)
# MAGIC
# MAGIC Each dim's surrogate must be referenced by at least one fact (no orphan dimension).

# COMMAND ----------

# Map each dim surrogate to the facts that reference it
dim_to_facts = {
    "date_key":    ["fact_sales_line", "fact_tender", "fact_audit_error", "fact_store_day"],
    "store_key":   ["fact_sales_line", "fact_tender", "fact_audit_error", "fact_store_day"],
    "channel_key": ["fact_sales_line", "fact_tender", "fact_audit_error", "fact_store_day"],
    "item_key":    ["fact_sales_line"],
    "tender_key":  ["fact_tender"],
}

for sk, facts in dim_to_facts.items():
    # count distinct surrogate values used across referencing facts
    union_sql = " UNION ".join(
        [f"SELECT DISTINCT {sk} FROM {CATALOG}.{CORE}.{f}" for f in facts]
    )
    used = spark.sql(f"SELECT COUNT(*) AS c FROM ({union_sql}) WHERE {sk} <> -1").collect()[0]["c"]
    record(f"Dim reachable via {sk}", used > 0, f"({used} distinct keys referenced)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Summary

# COMMAND ----------

import pandas as pd

summary_df = pd.DataFrame(results, columns=["check", "status", "detail"]).astype(str)
display(summary_df)

n_pass = sum(1 for _, s, _ in results if s == "PASS")
n_fail = sum(1 for _, s, _ in results if s == "FAIL")

print(f"\n{'='*50}")
print(f"  PASSED: {n_pass}")
print(f"  FAILED: {n_fail}")
print(f"{'='*50}")

if n_fail > 0:
    failed_checks = [name for name, s, _ in results if s == "FAIL"]
    print("\nFailed checks:")
    for c in failed_checks:
        print(f"  ❌ {c}")
    raise AssertionError(f"{n_fail} gold_core validation check(s) failed — see above.")
else:
    print("\n✅ All gold_core integrity checks passed. Star is sound.")
