# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R16 — Transaction with more than 50 item lines
# MAGIC
# MAGIC Size anomaly. A retail transaction with 50+ distinct item lines is unusual —
# MAGIC either a legitimate business/bulk order (worth knowing about) or a sign that
# MAGIC several transactions got merged into one by mistake.
# MAGIC
# MAGIC | Property | Value |
# MAGIC |---|---|
# MAGIC | **Rule ID** | `R16_OVERSIZED_BASKET` |
# MAGIC | **Severity** | `W` |
# MAGIC | **Inputs** | `sa_tran_item`, `sa_tran_head` |
# MAGIC | **Threshold** | `COUNT(items) > 50` per `TRAN_SEQ_NO` |
# MAGIC | **Scope** | per-transaction |

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import col, lit, concat, count as f_count
from pyspark.sql.types import DecimalType, StringType

# COMMAND ----------

# MAGIC %run ../_shared/rule_framework

# COMMAND ----------

RULE_ID   = "R16_OVERSIZED_BASKET"
RULE_NAME = "Transaction should have no more than 50 item lines"
SEVERITY  = Severity.WARNING

LINE_THRESHOLD = 50

# COMMAND ----------

if spark.catalog.tableExists(TARGET_TABLE):
    pre_count = spark.table(TARGET_TABLE).where(col("RULE_ID") == RULE_ID).count()
    if pre_count > 0:
        print(f"Deleting {pre_count:,} prior findings for {RULE_ID}.")
        spark.sql(f"DELETE FROM {TARGET_TABLE} WHERE RULE_ID = '{RULE_ID}'")

# COMMAND ----------

def run(spark) -> DataFrame:
    DECIMAL_TYPE = DecimalType(20, 4)

    line_counts = (
        spark.table("retaildp.silver.sa_tran_item")
        .groupBy("TRAN_SEQ_NO")
        .agg(f_count("*").alias("_line_count"))
        .where(col("_line_count") > LINE_THRESHOLD)
    )

    head = spark.table("retaildp.silver.sa_tran_head").select(
        "TRAN_SEQ_NO", "STORE", "BUSINESS_DATE", "RTLOG_ORIG_SYS",
    )

    violations = line_counts.join(head, "TRAN_SEQ_NO", "inner")

    narrow = violations.select(
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
        col("_line_count").cast(DECIMAL_TYPE).alias("MEASURED_VALUE"),
        lit(LINE_THRESHOLD).cast(DECIMAL_TYPE).alias("EXPECTED_VALUE"),
        (col("_line_count") - lit(LINE_THRESHOLD)).cast(DECIMAL_TYPE).alias("DELTA"),
        concat(
            lit("line_count="), col("_line_count").cast(StringType()),
            lit(" (threshold "), lit(LINE_THRESHOLD).cast(StringType()), lit(")"),
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

    print("Top 10 largest baskets:")
    this_rule.select("TRAN_SEQ_NO", "RTLOG_ORIG_SYS", "MEASURED_VALUE", "ERROR_DESC") \
        .orderBy(col("MEASURED_VALUE").desc()).show(10, truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Final sa_error overview — all 18 rules

# COMMAND ----------

if spark.catalog.tableExists(TARGET_TABLE):
    print(f"\n=== ALL RULES — FINAL STATE ===")
    print(f"Total findings: {spark.table(TARGET_TABLE).count():,}\n")
    spark.table(TARGET_TABLE).groupBy("RULE_ID", "SEVERITY").count().orderBy("RULE_ID").show(truncate=False)

    print("\nBy severity:")
    spark.table(TARGET_TABLE).groupBy("SEVERITY").count().orderBy("SEVERITY").show()

    print("\nBy channel:")
    spark.table(TARGET_TABLE).groupBy("RTLOG_ORIG_SYS").count().orderBy("RTLOG_ORIG_SYS").show()
