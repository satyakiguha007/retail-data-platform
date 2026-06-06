# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R07 — `ITEM_TYPE = 'ITM'` requires `UNIT_RETAIL > 0`
# MAGIC
# MAGIC Merchandise items must have a positive unit retail. Zero or NULL is legitimate
# MAGIC for non-merchandise types (`NMR` = freight/services, `REF` = reference items,
# MAGIC vouchers) but a merchandise sale at zero or NULL price is an audit signal —
# MAGIC either fault-injected, or a free-item promo that should have been recorded
# MAGIC as `NMR` instead.
# MAGIC
# MAGIC | Property | Value |
# MAGIC |---|---|
# MAGIC | **Rule ID** | `R07_ITM_UNIT_RETAIL_POSITIVE` |
# MAGIC | **Severity** | `M` |
# MAGIC | **Inputs** | `sa_tran_item`, `sa_tran_head` |
# MAGIC | **Scope** | TRAN-level (aggregates offending lines per transaction) |
# MAGIC | **Exempt** | All non-ITM ITEM_TYPEs (NMR, REF, VOUCHER, etc.) |

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import col, lit, concat, count as f_count
from pyspark.sql.types import DecimalType, StringType

# COMMAND ----------

# MAGIC %run ../_shared/rule_framework

# COMMAND ----------

RULE_ID   = "R07_ITM_UNIT_RETAIL_POSITIVE"
RULE_NAME = "ITEM_TYPE='ITM' requires UNIT_RETAIL > 0 (non-NULL and strictly positive)"
SEVERITY  = Severity.MINOR

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
        .where(col("ITEM_TYPE") == "ITM")
        .where(col("UNIT_RETAIL").isNull() | (col("UNIT_RETAIL") <= 0))
        .select("TRAN_SEQ_NO", "ITEM_SEQ_NO", "UNIT_RETAIL")
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
            lit("ITM lines with non-positive UNIT_RETAIL: "),
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
    this_rule.groupBy("RTLOG_ORIG_SYS").count().orderBy(col("count").desc()).show()
    this_rule.select("TRAN_SEQ_NO", "RTLOG_ORIG_SYS", "ERROR_DESC").limit(10).show(truncate=False)
