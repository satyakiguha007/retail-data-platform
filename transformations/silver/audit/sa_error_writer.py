# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Orchestrator — `sa_error_writer.py`
# MAGIC
# MAGIC Single entry point for Module 4. Runs all 18 audit rules in dependency order,
# MAGIC captures timing + errors per rule, then re-tags every finding written during
# MAGIC this batch with one shared `audit_run_id`. Prints a complete summary at the end.
# MAGIC
# MAGIC ## Design choice — `dbutils.notebook.run` over `%run`
# MAGIC
# MAGIC | Choice | Pro | Con |
# MAGIC |---|---|---|
# MAGIC | **`dbutils.notebook.run` (chosen)** | Iterable list, per-rule timing, exception isolation (one rule failing doesn't stop the rest) | Slightly slower (~1-2s per rule for notebook initialisation overhead) |
# MAGIC | `%run` | Faster, shared namespace | 18 separate cells, no error isolation, one failure aborts everything |
# MAGIC
# MAGIC ## audit_run_id sharing strategy
# MAGIC
# MAGIC Each rule writes findings independently with its own auto-generated run_id.
# MAGIC After all rules complete, the orchestrator runs a single `UPDATE` against
# MAGIC `sa_error` to set `_audit_run_id = BATCH_RUN_ID` for all rows where
# MAGIC `_audit_ts >= orchestrator_start_time`. Net effect: one batch = one run_id,
# MAGIC without modifying any rule file.
# MAGIC
# MAGIC ## Scheduling
# MAGIC
# MAGIC Put this notebook on a Databricks Job that depends on the silver-conformance
# MAGIC job. Each invocation = one complete audit pass with a fresh `audit_run_id`.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports

# COMMAND ----------

import time
from datetime import datetime
from pyspark.sql.functions import col

# COMMAND ----------

# MAGIC %md
# MAGIC ## Batch configuration

# COMMAND ----------

TARGET_TABLE = "retaildp.silver.sa_error"

orchestrator_start = datetime.now()
BATCH_RUN_ID       = f"batch_{orchestrator_start.strftime('%Y%m%d_%H%M%S')}"
START_TS_STR       = orchestrator_start.strftime("%Y-%m-%d %H:%M:%S")

print(f"Audit batch starting")
print(f"  BATCH_RUN_ID: {BATCH_RUN_ID}")
print(f"  Start time:   {orchestrator_start.isoformat()}")
print(f"  Target table: {TARGET_TABLE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Rule registry (execution order)
# MAGIC
# MAGIC Ordered by category. Rules are independent — no rule depends on another's output —
# MAGIC so the order is purely organisational, not technically required.

# COMMAND ----------

RULE_NOTEBOOKS = [
    # Reconciliation (Totals)
    "rules/01_store_day_count_reconcile",
    "rules/02_head_items_total",
    "rules/03_head_tender_total",
    "rules/04_disc_reasonable",

    # Sign / range integrity
    "rules/05_neg_qty_consistent",
    "rules/06_tender_sign_by_type",
    "rules/07_itm_unit_retail_positive",

    # Mandatory fields
    "rules/08_item_required",
    "rules/09_vendor_required_oms",
    "rules/10_tender_type_valid",

    # FK integrity
    "rules/11_head_store_day_fk",
    "rules/12_item_head_fk",
    "rules/13_tender_head_fk",

    # Reasonability
    "rules/14_value_3_sigma_outlier",
    "rules/15_single_line_dominates",
    "rules/16_oversized_basket",

    # Temporal / dimensional
    "rules/17_datetime_business_date_aligned",
    "rules/18_currency_matches_store",
]

print(f"\n{len(RULE_NOTEBOOKS)} rules registered for execution.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Execute all rules

# COMMAND ----------

results = []

for i, rule_notebook in enumerate(RULE_NOTEBOOKS, 1):
    print(f"\n[{i:2d}/{len(RULE_NOTEBOOKS)}] {rule_notebook}")
    rule_start = time.time()

    try:
        ret = dbutils.notebook.run(rule_notebook, timeout_seconds=600)
        elapsed = time.time() - rule_start
        results.append({
            "rule":      rule_notebook,
            "status":    "OK",
            "elapsed_s": round(elapsed, 1),
            "error":     None,
        })
        print(f"         OK  ({elapsed:.1f}s)")
    except Exception as e:
        elapsed = time.time() - rule_start
        results.append({
            "rule":      rule_notebook,
            "status":    "FAILED",
            "elapsed_s": round(elapsed, 1),
            "error":     str(e)[:300],   # truncate long stack traces
        })
        print(f"         FAILED  ({elapsed:.1f}s)  {str(e)[:200]}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Re-tag findings with shared `audit_run_id`

# COMMAND ----------

if not spark.catalog.tableExists(TARGET_TABLE):
    print(f"WARNING: {TARGET_TABLE} doesn't exist. No findings to tag.")
else:
    # Update only rows written during this batch — by timestamp threshold
    updated = spark.sql(f"""
        UPDATE {TARGET_TABLE}
        SET _audit_run_id = '{BATCH_RUN_ID}'
        WHERE _audit_ts >= TIMESTAMP '{START_TS_STR}'
    """)
    n_tagged = spark.table(TARGET_TABLE).where(col("_audit_run_id") == BATCH_RUN_ID).count()
    print(f"Re-tagged {n_tagged:,} findings with audit_run_id = {BATCH_RUN_ID}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Batch summary

# COMMAND ----------

orchestrator_end = datetime.now()
total_elapsed    = (orchestrator_end - orchestrator_start).total_seconds()
n_ok             = sum(1 for r in results if r["status"] == "OK")
n_failed         = sum(1 for r in results if r["status"] == "FAILED")

print("=" * 70)
print(f"AUDIT BATCH SUMMARY")
print("=" * 70)
print(f"BATCH_RUN_ID:  {BATCH_RUN_ID}")
print(f"Start:         {orchestrator_start.isoformat()}")
print(f"End:           {orchestrator_end.isoformat()}")
print(f"Total runtime: {total_elapsed:.1f}s")
print(f"Rules total:   {len(RULE_NOTEBOOKS)}")
print(f"  OK:          {n_ok}")
print(f"  FAILED:      {n_failed}")

# Per-rule timing
print("\nPer-rule timing:")
print(f"  {'Rule':<55s} {'Status':<8s} {'Elapsed':>10s}")
print(f"  {'-'*55} {'-'*8} {'-'*10}")
for r in results:
    name_short = r["rule"].replace("rules/", "")[:55]
    print(f"  {name_short:<55s} {r['status']:<8s} {r['elapsed_s']:>8.1f}s")

# Failed rules detail
if n_failed > 0:
    print(f"\n*** {n_failed} RULE(S) FAILED ***")
    for r in results:
        if r["status"] == "FAILED":
            print(f"  {r['rule']}:")
            print(f"    {r['error']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Findings written this batch

# COMMAND ----------

if spark.catalog.tableExists(TARGET_TABLE):
    this_batch = spark.table(TARGET_TABLE).where(col("_audit_run_id") == BATCH_RUN_ID)
    n_findings = this_batch.count()

    print(f"Findings in this batch: {n_findings:,}\n")

    if n_findings > 0:
        print("By rule + severity:")
        this_batch.groupBy("RULE_ID", "SEVERITY").count().orderBy("RULE_ID").show(truncate=False)

        print("By severity:")
        this_batch.groupBy("SEVERITY").count().orderBy("SEVERITY").show()

        print("By channel:")
        this_batch.groupBy("RTLOG_ORIG_SYS").count().orderBy("RTLOG_ORIG_SYS").show()

        print("By severity × channel:")
        this_batch.groupBy("SEVERITY", "RTLOG_ORIG_SYS").count() \
            .orderBy("SEVERITY", "RTLOG_ORIG_SYS").show()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Final state — all rules across all historical batches

# COMMAND ----------

if spark.catalog.tableExists(TARGET_TABLE):
    sa_error = spark.table(TARGET_TABLE)
    print(f"Total findings in {TARGET_TABLE}: {sa_error.count():,}\n")

    print("Distinct audit runs recorded:")
    sa_error.groupBy("_audit_run_id").count().orderBy(col("_audit_run_id").desc()).show(10, truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Exit
# MAGIC
# MAGIC In a Databricks Job context, callers can read the `BATCH_RUN_ID` via
# MAGIC `dbutils.notebook.exit()`. Useful for downstream jobs (e.g., Power BI refresh)
# MAGIC that need to know which audit batch they're consuming.

# COMMAND ----------

dbutils.notebook.exit(BATCH_RUN_ID)
