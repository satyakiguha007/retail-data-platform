# Databricks notebook source
# MAGIC %md
# MAGIC # Audit — `sa_error` schema (single source of truth)
# MAGIC
# MAGIC Defines the canonical schema for the audit findings table. Imported via `%run`
# MAGIC by `rule_framework.py` (and transitively by every rule notebook). Keeping the
# MAGIC schema in one place prevents drift between rules — if we add a column here,
# MAGIC every rule picks it up on next `%run`.
# MAGIC
# MAGIC ## Two schemas live here
# MAGIC
# MAGIC | Schema | Used by | Role |
# MAGIC |---|---|---|
# MAGIC | `sa_error_schema` | the target Delta table | Full SA_ERROR shape — 14 cols |
# MAGIC | `narrow_finding_schema` | each rule's `run()` output | The 8 cols rules MUST produce; the framework stamps the remaining 6 |
# MAGIC
# MAGIC The split exists so individual rule code stays focused on the business logic
# MAGIC (what to flag, with what numbers) and doesn't repeat boilerplate for
# MAGIC ERROR_SEQ_NO hashing, RULE_ID stamping, timestamps, etc.

# COMMAND ----------

from pyspark.sql.types import (
    StructType, StructField, LongType, StringType,
    DateType, TimestampType, DecimalType,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## `sa_error_schema` — full SA_ERROR shape

# COMMAND ----------

sa_error_schema = StructType([
    # Identity
    StructField("ERROR_SEQ_NO",   LongType(),         nullable=False),   # PK = xxhash64(TRAN_SEQ_NO, RULE_ID)

    # Context (denormalized for fast filtering / Power BI joins later)
    StructField("TRAN_SEQ_NO",    LongType(),         nullable=False),   # FK → sa_tran_head
    StructField("STORE",          LongType(),         nullable=False),
    StructField("BUSINESS_DATE",  DateType(),         nullable=False),   # Partition key
    StructField("RTLOG_ORIG_SYS", StringType(),       nullable=False),   # 'POS' / 'MKT' / 'OMS'

    # Rule metadata
    StructField("RULE_ID",        StringType(),       nullable=False),   # e.g. 'R02_HEAD_ITEMS_TOTAL'
    StructField("RULE_NAME",      StringType(),       nullable=False),   # human-readable summary
    StructField("SEVERITY",       StringType(),       nullable=False),   # 'W' / 'M' / 'F' (ReSA convention)

    # Numeric evidence
    StructField("MEASURED_VALUE", DecimalType(20, 4), nullable=True),    # what the rule observed
    StructField("EXPECTED_VALUE", DecimalType(20, 4), nullable=True),    # what the rule expected
    StructField("DELTA",          DecimalType(20, 4), nullable=True),    # expected − measured

    # Narrative
    StructField("ERROR_DESC",     StringType(),       nullable=False),   # "head.VALUE=400.00, SUM(items)=395.50, delta=4.50"

    # Lineage
    StructField("_audit_ts",      TimestampType(),    nullable=False),   # when this rule evaluated this row
    StructField("_audit_run_id",  StringType(),       nullable=False),   # batch identifier (stamped at write)
])

SA_ERROR_COLUMNS = [f.name for f in sa_error_schema.fields]

# COMMAND ----------

# MAGIC %md
# MAGIC ## `narrow_finding_schema` — what each rule's `run()` MUST produce
# MAGIC
# MAGIC The 8 columns below are the contract between rules and the framework.
# MAGIC Each rule's `run()` returns a DataFrame with exactly these columns (extras
# MAGIC are silently dropped). `emit_findings()` in the framework takes that narrow
# MAGIC output, stamps the 6 framework-controlled columns (ERROR_SEQ_NO hash,
# MAGIC RULE_ID/RULE_NAME/SEVERITY, `_audit_ts`, `_audit_run_id` placeholder), and
# MAGIC returns a fully sa_error-shaped DataFrame.

# COMMAND ----------

narrow_finding_schema = StructType([
    StructField("TRAN_SEQ_NO",    LongType(),         nullable=False),
    StructField("STORE",          LongType(),         nullable=False),
    StructField("BUSINESS_DATE",  DateType(),         nullable=False),
    StructField("RTLOG_ORIG_SYS", StringType(),       nullable=False),
    StructField("MEASURED_VALUE", DecimalType(20, 4), nullable=True),
    StructField("EXPECTED_VALUE", DecimalType(20, 4), nullable=True),
    StructField("DELTA",          DecimalType(20, 4), nullable=True),
    StructField("ERROR_DESC",     StringType(),       nullable=False),
])

NARROW_FINDING_COLUMNS = [f.name for f in narrow_finding_schema.fields]

# COMMAND ----------

print(f"sa_error_schema:       {len(SA_ERROR_COLUMNS)} columns — {SA_ERROR_COLUMNS}")
print(f"narrow_finding_schema: {len(NARROW_FINDING_COLUMNS)} columns — {NARROW_FINDING_COLUMNS}")

