# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R02 — `head.VALUE` = SUM(item.QTY × UNIT_RETAIL) − SUM(disc.QTY × UNIT_DISCOUNT_AMT)
# MAGIC
# MAGIC Net head-line reconciliation. Items and discounts both use per-unit pricing
# MAGIC (`QTY × UNIT_*` line totals), summed per transaction. Tax tables are NOT in the
# MAGIC formula — ReSA convention treats `sa_tran_igtax` / `sa_tran_tax` as informational
# MAGIC breakdowns OF `head.VALUE`, not separate variables to add or subtract.
# MAGIC
# MAGIC | Property | Value |
# MAGIC |---|---|
# MAGIC | **Rule ID** | `R02_HEAD_ITEMS_TOTAL` |
# MAGIC | **Severity** | `F` (fatal) |
# MAGIC | **Tolerance** | `0.0` — hard equality (locked decision B) |
# MAGIC | **Inputs** | `sa_tran_head`, `sa_tran_item`, `sa_tran_disc` (left-joined for COALESCE-to-zero) |
# MAGIC
# MAGIC ## Column names (verified from project schema docs)
# MAGIC
# MAGIC | Table | Column used | Line total |
# MAGIC |---|---|---|
# MAGIC | `sa_tran_item` | `QTY`, `UNIT_RETAIL` | `QTY × UNIT_RETAIL` |
# MAGIC | `sa_tran_disc` | `QTY`, `UNIT_DISCOUNT_AMT` | `QTY × UNIT_DISCOUNT_AMT` |
# MAGIC
# MAGIC ## Expected residual (genuine audit findings)
# MAGIC - POS RETURN with `head=0` convention (~527)
# MAGIC - POS PVOID with `head≠items` convention (~13)
# MAGIC - Any true fault-injected anomalies
# MAGIC - MKT findings: 0 (disc-aware)
# MAGIC - OMS findings: 0 (head=items by construction)
# MAGIC
# MAGIC Approximate total: ~540 — the same number that first surfaced in the original
# MAGIC "POS without disc" diagnostic. Those rows were always the real answer.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & framework

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, lit, concat, coalesce, when,
    sum as f_sum,
)
from pyspark.sql.types import DecimalType, StringType

# COMMAND ----------

# MAGIC %run ../_shared/rule_framework

# COMMAND ----------

# MAGIC %md
# MAGIC ## Rule constants

# COMMAND ----------

RULE_ID   = "R02_HEAD_ITEMS_TOTAL"
RULE_NAME = "sa_tran_head.VALUE == SUM(item.QTY * UNIT_RETAIL) - SUM(disc.QTY * UNIT_DISCOUNT_AMT) per transaction"
SEVERITY  = Severity.FATAL

TOLERANCE = 0.0   # hard equality — locked decision B

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pre-run cleanup

# COMMAND ----------

if spark.catalog.tableExists(TARGET_TABLE):
    pre_count = spark.table(TARGET_TABLE).where(col("RULE_ID") == RULE_ID).count()
    if pre_count > 0:
        print(f"Deleting {pre_count:,} prior findings for {RULE_ID}.")
        spark.sql(f"DELETE FROM {TARGET_TABLE} WHERE RULE_ID = '{RULE_ID}'")
    else:
        print(f"No prior findings for {RULE_ID}.")
else:
    print(f"{TARGET_TABLE} doesn't exist yet — first-time run.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Rule logic — `run(spark)`

# COMMAND ----------

def run(spark) -> DataFrame:
    """head.VALUE == SUM(items) - SUM(disc). Both per-unit × QTY."""
    DECIMAL_TYPE = DecimalType(20, 4)
    ZERO         = lit(0).cast(DECIMAL_TYPE)

    head = spark.table("retaildp.silver.sa_tran_head").select(
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
        col("VALUE").cast(DECIMAL_TYPE).alias("_head_value"),
    )

    item_totals = (
        spark.table("retaildp.silver.sa_tran_item")
        .selectExpr(
            "TRAN_SEQ_NO",
            "CAST(QTY * UNIT_RETAIL AS DECIMAL(20, 4)) AS _line_total",
        )
        .groupBy("TRAN_SEQ_NO")
        .agg(f_sum("_line_total").cast(DECIMAL_TYPE).alias("_item_sum"))
    )

    disc_totals = (
        spark.table("retaildp.silver.sa_tran_disc")
        .selectExpr(
            "TRAN_SEQ_NO",
            "CAST(QTY * UNIT_DISCOUNT_AMT AS DECIMAL(20, 4)) AS _line_disc",
        )
        .groupBy("TRAN_SEQ_NO")
        .agg(f_sum("_line_disc").cast(DECIMAL_TYPE).alias("_disc_sum"))
    )

    drift = (
        head
        .join(item_totals, "TRAN_SEQ_NO", "inner")
        .join(disc_totals, "TRAN_SEQ_NO", "left")
        .withColumn("_disc_sum", coalesce(col("_disc_sum"), ZERO))
        .withColumn("_expected", (col("_item_sum") - col("_disc_sum")).cast(DECIMAL_TYPE))
        .withColumn("_delta",    (col("_head_value") - col("_expected")).cast(DECIMAL_TYPE))
        .where(col("_delta") != ZERO)
    )

    narrow = drift.select(
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
        col("_expected").alias("MEASURED_VALUE"),
        col("_head_value").alias("EXPECTED_VALUE"),
        col("_delta").alias("DELTA"),
        concat(
            lit("head="),       col("_head_value").cast(StringType()),
            lit(", items="),    col("_item_sum").cast(StringType()),
            lit(", disc="),     col("_disc_sum").cast(StringType()),
            lit(", expected="), col("_expected").cast(StringType()),
            lit(", delta="),    col("_delta").cast(StringType()),
        ).alias("ERROR_DESC"),
    )

    return emit_findings(narrow, RULE_ID, RULE_NAME, SEVERITY)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Execute

# COMMAND ----------

findings = run(spark)
n_written = write_findings(findings)
print(f"\n{RULE_ID}: {n_written} finding(s) written to {TARGET_TABLE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Validation

# COMMAND ----------

print(f"=== Validation: {RULE_ID} (v5 — final, project-schema-verified) ===\n")

total_txns = spark.table("retaildp.silver.sa_tran_head").count()
this_rule  = spark.table(TARGET_TABLE).where(col("RULE_ID") == RULE_ID)
flagged    = this_rule.count()

print(f"Total transactions in sa_tran_head: {total_txns:,}")
print(f"Flagged by {RULE_ID}:                {flagged:,}  ({100.0 * flagged / total_txns:.4f}%)")
print(f"Clean for this rule:                 {total_txns - flagged:,}")

if flagged > 0:
    print("\nFindings by channel × TRAN_TYPE × head bucket:")
    (
        this_rule.alias("e")
        .join(
            spark.table("retaildp.silver.sa_tran_head").select("TRAN_SEQ_NO", "TRAN_TYPE", "VALUE").alias("h"),
            "TRAN_SEQ_NO",
        )
        .withColumn(
            "head_bucket",
            when(col("VALUE") == 0, lit("head=0"))
            .when(col("VALUE") > 0, lit("head>0"))
            .otherwise(lit("head<0"))
        )
        .groupBy("RTLOG_ORIG_SYS", "TRAN_TYPE", "head_bucket")
        .count()
        .orderBy(col("count").desc())
        .show(truncate=False)
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## sa_error overview

# COMMAND ----------

if spark.catalog.tableExists(TARGET_TABLE):
    print(f"Total findings across all rules: {spark.table(TARGET_TABLE).count():,}\n")
    spark.table(TARGET_TABLE).groupBy("RULE_ID", "SEVERITY").count().orderBy("RULE_ID").show(truncate=False)
