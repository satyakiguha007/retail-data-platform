# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R05 — Negative `QTY` only on `TRAN_TYPE` ∈ {RETURN, CREFUND}
# MAGIC
# MAGIC ReSA sign convention: only return-class transactions carry negative item
# MAGIC quantities. A SALE/PVOID/NOSALE/etc. with negative `item.QTY` is a real
# MAGIC integrity break — either fault-injected, or a return mistakenly logged as a SALE.
# MAGIC
# MAGIC | Property | Value |
# MAGIC |---|---|
# MAGIC | **Rule ID** | `R05_NEG_QTY_CONSISTENT` |
# MAGIC | **Severity** | `F` (fatal — wrong-signed quantity will break gold-layer aggregations) |
# MAGIC | **Inputs** | `sa_tran_item`, `sa_tran_head` |
# MAGIC | **Allowed types** | `RETURN`, `CREFUND` |
# MAGIC | **Scope** | TRAN-level (aggregates offending lines per transaction) |

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, lit, concat,
    sum as f_sum,
    count as f_count,
)
from pyspark.sql.types import DecimalType, StringType

# COMMAND ----------

# MAGIC %run ../_shared/rule_framework

# COMMAND ----------

RULE_ID   = "R05_NEG_QTY_CONSISTENT"
RULE_NAME = "Negative item.QTY allowed only on TRAN_TYPE in {RETURN, CREFUND}"
SEVERITY  = Severity.FATAL

ALLOWED_NEG_TYPES = ["RETURN", "CREFUND"]

# COMMAND ----------

if spark.catalog.tableExists(TARGET_TABLE):
    pre_count = spark.table(TARGET_TABLE).where(col("RULE_ID") == RULE_ID).count()
    if pre_count > 0:
        print(f"Deleting {pre_count:,} prior findings for {RULE_ID}.")
        spark.sql(f"DELETE FROM {TARGET_TABLE} WHERE RULE_ID = '{RULE_ID}'")

# COMMAND ----------

def run(spark) -> DataFrame:
    DECIMAL_TYPE = DecimalType(20, 4)

    item_negs = (
        spark.table("retaildp.silver.sa_tran_item")
        .where(col("QTY") < 0)
        .select("TRAN_SEQ_NO", "ITEM_SEQ_NO", col("QTY").alias("_neg_qty"))
    )

    head = spark.table("retaildp.silver.sa_tran_head").select(
        "TRAN_SEQ_NO", "STORE", "BUSINESS_DATE", "RTLOG_ORIG_SYS", "TRAN_TYPE",
    )

    violations = (
        item_negs.join(head, "TRAN_SEQ_NO", "inner")
        .where(~col("TRAN_TYPE").isin(ALLOWED_NEG_TYPES))
        .groupBy("TRAN_SEQ_NO", "STORE", "BUSINESS_DATE", "RTLOG_ORIG_SYS", "TRAN_TYPE")
        .agg(
            f_count("*").alias("_neg_line_count"),
            f_sum(col("_neg_qty")).alias("_total_neg_qty"),
        )
    )

    narrow = violations.select(
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
        col("_neg_line_count").cast(DECIMAL_TYPE).alias("MEASURED_VALUE"),
        lit(0).cast(DECIMAL_TYPE).alias("EXPECTED_VALUE"),
        col("_neg_line_count").cast(DECIMAL_TYPE).alias("DELTA"),
        concat(
            lit("TRAN_TYPE="),       col("TRAN_TYPE"),
            lit(", neg_lines="),     col("_neg_line_count").cast(StringType()),
            lit(", total_neg_qty="), col("_total_neg_qty").cast(StringType()),
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

    print("Sample findings:")
    this_rule.select("TRAN_SEQ_NO", "RTLOG_ORIG_SYS", "ERROR_DESC").limit(10).show(truncate=False)
