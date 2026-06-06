# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R08 — `sa_tran_item.ITEM` is non-null and non-blank
# MAGIC
# MAGIC Mandatory-field check. Every item line — whether merchandise (`ITM`),
# MAGIC non-merchandise (`NMR`), reference, or voucher — must carry a non-blank
# MAGIC identifier in the `ITEM` column. Without it, downstream gold-layer joins
# MAGIC to product dimensions can't resolve.
# MAGIC
# MAGIC | Property | Value |
# MAGIC |---|---|
# MAGIC | **Rule ID** | `R08_ITEM_REQUIRED` |
# MAGIC | **Severity** | `F` (fatal) |
# MAGIC | **Inputs** | `sa_tran_item`, `sa_tran_head` (for context) |
# MAGIC | **Scope** | TRAN-level (aggregates offending lines per transaction) |
# MAGIC
# MAGIC ## Expected result
# MAGIC
# MAGIC `0` if silver DQ enforced this at write time. R08 is the formal audit version —
# MAGIC running it cleanly is itself a useful audit assertion.

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, lit, concat, trim,
    count as f_count,
)
from pyspark.sql.types import DecimalType, StringType

# COMMAND ----------

# MAGIC %run ../_shared/rule_framework

# COMMAND ----------

RULE_ID   = "R08_ITEM_REQUIRED"
RULE_NAME = "sa_tran_item.ITEM must be non-null and non-blank for every item line"
SEVERITY  = Severity.FATAL

# COMMAND ----------

if spark.catalog.tableExists(TARGET_TABLE):
    pre_count = spark.table(TARGET_TABLE).where(col("RULE_ID") == RULE_ID).count()
    if pre_count > 0:
        print(f"Deleting {pre_count:,} prior findings for {RULE_ID}.")
        spark.sql(f"DELETE FROM {TARGET_TABLE} WHERE RULE_ID = '{RULE_ID}'")

# COMMAND ----------

def run(spark) -> DataFrame:
    DECIMAL_TYPE = DecimalType(20, 4)

    bad_lines = (
        spark.table("retaildp.silver.sa_tran_item")
        .where(col("ITEM").isNull() | (trim(col("ITEM")) == ""))
        .select("TRAN_SEQ_NO", "ITEM_SEQ_NO", "ITEM_TYPE")
    )

    head = spark.table("retaildp.silver.sa_tran_head").select(
        "TRAN_SEQ_NO", "STORE", "BUSINESS_DATE", "RTLOG_ORIG_SYS",
    )

    violations = (
        bad_lines.join(head, "TRAN_SEQ_NO", "inner")
        .groupBy("TRAN_SEQ_NO", "STORE", "BUSINESS_DATE", "RTLOG_ORIG_SYS")
        .agg(f_count("*").alias("_bad_line_count"))
    )

    narrow = violations.select(
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
        col("_bad_line_count").cast(DECIMAL_TYPE).alias("MEASURED_VALUE"),
        lit(0).cast(DECIMAL_TYPE).alias("EXPECTED_VALUE"),
        col("_bad_line_count").cast(DECIMAL_TYPE).alias("DELTA"),
        concat(
            lit("Item lines missing ITEM: "),
            col("_bad_line_count").cast(StringType()),
        ).alias("ERROR_DESC"),
    )

    return emit_findings(narrow, RULE_ID, RULE_NAME, SEVERITY)

# COMMAND ----------

findings = run(spark)
n_written = write_findings(findings)
print(f"\n{RULE_ID}: {n_written} finding(s) written to {TARGET_TABLE}")

# COMMAND ----------

print(f"=== Validation: {RULE_ID} ===\n")
this_rule = spark.table(TARGET_TABLE).where(col("RULE_ID") == RULE_ID)
flagged   = this_rule.count()
print(f"Flagged: {flagged:,}")

if flagged > 0:
    print("\nFindings by channel:")
    this_rule.groupBy("RTLOG_ORIG_SYS").count().orderBy(col("count").desc()).show()
    this_rule.select("TRAN_SEQ_NO", "RTLOG_ORIG_SYS", "ERROR_DESC").limit(10).show(truncate=False)
else:
    print("Rule passed cleanly — every item line has a non-blank ITEM identifier.")
