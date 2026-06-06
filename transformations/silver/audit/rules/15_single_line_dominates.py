# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R15 — Single item line > 80% of transaction value
# MAGIC
# MAGIC Line-concentration anomaly. A transaction where one item line accounts for
# MAGIC more than 80% of the total value is unusual — either a single high-value item
# MAGIC (legitimate but worth review) or a data-entry error (one line at 1000× the
# MAGIC intended price). Auditor judgment required.
# MAGIC
# MAGIC | Property | Value |
# MAGIC |---|---|
# MAGIC | **Rule ID** | `R15_SINGLE_LINE_DOMINATES` |
# MAGIC | **Severity** | `W` |
# MAGIC | **Inputs** | `sa_tran_item`, `sa_tran_head` |
# MAGIC | **Method** | window over `TRAN_SEQ_NO` to compute line share |
# MAGIC | **Threshold** | line_share > 0.80 |
# MAGIC | **Min lines** | 5+ (filters small-basket noise; MKT's 2-line "main item + fee" convention always shows 99% dominance) |
# MAGIC | **Excludes** | transactions where tran_total is 0 or negative (R02/R05 handle those) |

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.window import Window
from pyspark.sql.functions import (
    col, lit, concat, abs as f_abs,
    sum as f_sum, count as f_count, max as f_max,
)
from pyspark.sql.types import DecimalType, StringType

# COMMAND ----------

# MAGIC %run ../_shared/rule_framework

# COMMAND ----------

RULE_ID   = "R15_SINGLE_LINE_DOMINATES"
RULE_NAME = "No single item line should account for > 80% of transaction gross value"
SEVERITY  = Severity.WARNING

DOMINANCE_THRESHOLD = 0.80
MIN_LINE_COUNT      = 5    # Require 5+ lines; filters out MKT's 2-line convention (main item + tiny fee)

# COMMAND ----------

if spark.catalog.tableExists(TARGET_TABLE):
    pre_count = spark.table(TARGET_TABLE).where(col("RULE_ID") == RULE_ID).count()
    if pre_count > 0:
        print(f"Deleting {pre_count:,} prior findings for {RULE_ID}.")
        spark.sql(f"DELETE FROM {TARGET_TABLE} WHERE RULE_ID = '{RULE_ID}'")

# COMMAND ----------

def run(spark) -> DataFrame:
    DECIMAL_TYPE = DecimalType(20, 4)
    w            = Window.partitionBy("TRAN_SEQ_NO")

    line_shares = (
        spark.table("retaildp.silver.sa_tran_item")
        .selectExpr(
            "TRAN_SEQ_NO",
            "ITEM_SEQ_NO",
            "CAST(QTY * UNIT_RETAIL AS DECIMAL(20, 4)) AS _line_total",
        )
        .withColumn("_tran_total", f_sum(col("_line_total")).over(w))
        .withColumn("_line_count", f_count("*").over(w))
        .where(col("_line_count") >= MIN_LINE_COUNT)                         # exclude small-basket noise (MKT 2-line convention)
        .where(f_abs(col("_tran_total")) > 0)                                # avoid div-by-zero
        .withColumn(
            "_line_share",
            (f_abs(col("_line_total")) / f_abs(col("_tran_total"))).cast(DecimalType(10, 4)),
        )
        .where(col("_line_share") > DOMINANCE_THRESHOLD)
    )

    # Aggregate to TRAN level — one finding per transaction with at least one dominating line
    head = spark.table("retaildp.silver.sa_tran_head").select(
        "TRAN_SEQ_NO", "STORE", "BUSINESS_DATE", "RTLOG_ORIG_SYS",
    )

    violations = (
        line_shares.join(head, "TRAN_SEQ_NO", "inner")
        .groupBy("TRAN_SEQ_NO", "STORE", "BUSINESS_DATE", "RTLOG_ORIG_SYS")
        .agg(
            f_max(col("_line_share")).alias("_max_share"),
            f_max(col("_line_total")).alias("_max_line_total"),
            f_max(col("_tran_total")).alias("_tran_total"),
        )
    )

    narrow = violations.select(
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
        col("_max_share").cast(DECIMAL_TYPE).alias("MEASURED_VALUE"),
        lit(DOMINANCE_THRESHOLD).cast(DECIMAL_TYPE).alias("EXPECTED_VALUE"),
        (col("_max_share") - lit(DOMINANCE_THRESHOLD)).cast(DECIMAL_TYPE).alias("DELTA"),
        concat(
            lit("max_line_share="), col("_max_share").cast(StringType()),
            lit(", line_total="),   col("_max_line_total").cast(StringType()),
            lit(", tran_total="),   col("_tran_total").cast(StringType()),
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

    print("Top 10 by dominance:")
    this_rule.select(
        "TRAN_SEQ_NO", "RTLOG_ORIG_SYS", "MEASURED_VALUE", "ERROR_DESC",
    ).orderBy(col("MEASURED_VALUE").desc()).show(10, truncate=False)
