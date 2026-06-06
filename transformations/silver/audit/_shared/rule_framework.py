# Databricks notebook source
# MAGIC %md
# MAGIC # Audit — rule framework
# MAGIC
# MAGIC Generic scaffold every rule notebook depends on. Provides:
# MAGIC
# MAGIC | Symbol | Role |
# MAGIC |---|---|
# MAGIC | `Severity.WARNING / MINOR / FATAL` | ReSA-style severity constants `'W'` / `'M'` / `'F'` |
# MAGIC | `TARGET_TABLE` | `retaildp.silver.sa_error` — the audit findings table |
# MAGIC | `emit_findings(narrow_df, rule_id, rule_name, severity)` | Take a rule's narrow output, stamp the framework-controlled columns, return SA_ERROR-shaped DataFrame |
# MAGIC | `write_findings(findings, target_table=, audit_run_id=)` | MERGE findings into SA_ERROR; bootstrap target if missing; idempotent on re-runs |
# MAGIC
# MAGIC ## How a rule uses the framework
# MAGIC
# MAGIC ```
# MAGIC %run ../_shared/rule_framework
# MAGIC
# MAGIC RULE_ID, RULE_NAME, SEVERITY = "R02_…", "head=items", Severity.FATAL
# MAGIC
# MAGIC def run(spark):
# MAGIC     narrow = (...).select(TRAN_SEQ_NO, STORE, BUSINESS_DATE, RTLOG_ORIG_SYS,
# MAGIC                           MEASURED_VALUE, EXPECTED_VALUE, DELTA, ERROR_DESC)
# MAGIC     return emit_findings(narrow, RULE_ID, RULE_NAME, SEVERITY)
# MAGIC
# MAGIC findings = run(spark)
# MAGIC write_findings(findings)
# MAGIC ```
# MAGIC
# MAGIC ## Idempotency
# MAGIC
# MAGIC `ERROR_SEQ_NO = xxhash64(TRAN_SEQ_NO, RULE_ID)` — deterministic. Re-running
# MAGIC the same rule on the same data MERGEs over the same PKs, no duplicates.
# MAGIC If a previously-flagged transaction is fixed and re-conformed, the rule
# MAGIC simply won't fire on it; the stale finding remains in `sa_error` as a
# MAGIC historical record (filter by `_audit_run_id` for "current open findings").

# COMMAND ----------

import time
from typing import Optional
from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, lit, current_timestamp, xxhash64,
)
from pyspark.sql.types import StringType
from delta.tables import DeltaTable

# COMMAND ----------

# MAGIC %md
# MAGIC ## Schema dependency (transitive — exposed to caller via %run cascade)

# COMMAND ----------

# MAGIC %run ./sa_error_schema

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

TARGET_TABLE = "retaildp.silver.sa_error"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Severity constants (ReSA convention)

# COMMAND ----------

class Severity:
    """ReSA error severity codes. F is blocking, M needs auditor action, W is informational."""
    WARNING = "W"
    MINOR   = "M"
    FATAL   = "F"

VALID_SEVERITIES = {Severity.WARNING, Severity.MINOR, Severity.FATAL}

# COMMAND ----------

# MAGIC %md
# MAGIC ## `emit_findings` — narrow → SA_ERROR-shaped

# COMMAND ----------

def emit_findings(
    narrow_df: DataFrame,
    rule_id: str,
    rule_name: str,
    severity: str,
) -> DataFrame:
    """
    Convert a rule's narrow finding output into a fully sa_error-shaped DataFrame.

    The narrow DataFrame must conform to `narrow_finding_schema` (8 columns).
    This function adds the 6 framework-controlled columns:
      - ERROR_SEQ_NO (PK, hash of TRAN_SEQ_NO + RULE_ID)
      - RULE_ID, RULE_NAME, SEVERITY (rule metadata)
      - _audit_ts (now), _audit_run_id (NULL placeholder — write_findings stamps)

    Raises
    ------
    ValueError if severity is invalid or narrow_df is missing required columns.
    """
    if severity not in VALID_SEVERITIES:
        raise ValueError(
            f"severity must be one of {sorted(VALID_SEVERITIES)}, got {severity!r}"
        )

    missing = set(NARROW_FINDING_COLUMNS) - set(narrow_df.columns)
    if missing:
        raise ValueError(
            f"narrow_df is missing required columns: {sorted(missing)}. "
            f"Expected: {NARROW_FINDING_COLUMNS}"
        )

    from pyspark.sql.types import DecimalType   # local import keeps cell self-contained

    return narrow_df.select(
        xxhash64(col("TRAN_SEQ_NO"), lit(rule_id)).alias("ERROR_SEQ_NO"),
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
        lit(rule_id).alias("RULE_ID"),
        lit(rule_name).alias("RULE_NAME"),
        lit(severity).alias("SEVERITY"),
        col("MEASURED_VALUE").cast(DecimalType(20, 4)).alias("MEASURED_VALUE"),
        col("EXPECTED_VALUE").cast(DecimalType(20, 4)).alias("EXPECTED_VALUE"),
        col("DELTA").cast(DecimalType(20, 4)).alias("DELTA"),
        col("ERROR_DESC"),
        current_timestamp().alias("_audit_ts"),
        lit(None).cast(StringType()).alias("_audit_run_id"),   # write_findings stamps
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## `write_findings` — MERGE into SA_ERROR (bootstrap-on-first-write)

# COMMAND ----------

def write_findings(
    findings: DataFrame,
    target_table: str = TARGET_TABLE,
    audit_run_id: Optional[str] = None,
) -> int:
    """
    MERGE findings into the SA_ERROR Delta table.

    - Generates `audit_run_id` from current time if None (standalone rule mode).
    - Orchestrators pass a single `audit_run_id` to tag all findings from one batch.
    - Bootstraps the target table partitioned by BUSINESS_DATE on first write.
    - MERGE on ERROR_SEQ_NO PK — idempotent on re-runs of the same rule + data.

    Returns the number of findings the rule produced (pre-MERGE count).
    """
    if audit_run_id is None:
        audit_run_id = f"run_{int(time.time())}"

    stamped = findings.withColumn("_audit_run_id", lit(audit_run_id))

    n = stamped.count()
    if n == 0:
        print(f"No findings to write (audit_run_id={audit_run_id})")
        # Still bootstrap the empty target if absent, so downstream queries don't fail
        if not spark.catalog.tableExists(target_table):
            print(f"Bootstrap (empty): creating {target_table}")
            empty = spark.createDataFrame([], sa_error_schema)
            (
                empty.write
                .format("delta")
                .partitionBy("BUSINESS_DATE")
                .option("delta.autoOptimize.optimizeWrite", "true")
                .option("delta.autoOptimize.autoCompact",   "true")
                .saveAsTable(target_table)
            )
        return 0

    # Project to exact schema order before write
    projected = stamped.select(*SA_ERROR_COLUMNS)

    if not spark.catalog.tableExists(target_table):
        print(f"Bootstrap: creating {target_table} partitioned by BUSINESS_DATE")
        (
            projected.write
            .format("delta")
            .partitionBy("BUSINESS_DATE")
            .option("delta.autoOptimize.optimizeWrite", "true")
            .option("delta.autoOptimize.autoCompact",   "true")
            .saveAsTable(target_table)
        )
        print(f"  Inserted {n} findings (table created)")
    else:
        print(f"MERGE: {n} findings into {target_table} (audit_run_id={audit_run_id})")
        target = DeltaTable.forName(spark, target_table)
        (
            target.alias("t")
            .merge(projected.alias("s"), "t.ERROR_SEQ_NO = s.ERROR_SEQ_NO")
            .whenMatchedUpdateAll()
            .whenNotMatchedInsertAll()
            .execute()
        )

    return n

# COMMAND ----------

# MAGIC %md
# MAGIC ## Sanity print on %run

# COMMAND ----------

print("rule_framework loaded.")
print(f"  TARGET_TABLE: {TARGET_TABLE}")
print(f"  Severity:     WARNING={Severity.WARNING!r}, MINOR={Severity.MINOR!r}, FATAL={Severity.FATAL!r}")
print(f"  Helpers:      emit_findings(), write_findings()")

