# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R03 — `head.VALUE` ≈ SUM(`sa_tran_tender.TENDER_AMT`)
# MAGIC
# MAGIC Payment reconciliation. The head-line transaction value must match the sum of
# MAGIC all tender amounts within a 1% tolerance band. Where they disagree by more
# MAGIC than 1%, something is off — either payment data dropped, head value is stale,
# MAGIC or there's a promotional adjustment baked into payment but not into items.
# MAGIC
# MAGIC | Property | Value |
# MAGIC |---|---|
# MAGIC | **Rule ID** | `R03_HEAD_TENDER_TOTAL` |
# MAGIC | **Severity** | `M` (minor — locked decision A) |
# MAGIC | **Tolerance** | 1% — `abs(delta) > abs(head.VALUE) * 0.01` |
# MAGIC | **Inputs** | `sa_tran_head`, `sa_tran_tender` (LEFT JOIN) |
# MAGIC | **Verified columns** | `sa_tran_tender.TENDER_AMT` (DecimalType(20,4), nullable=False) |
# MAGIC
# MAGIC ## Tolerance edge cases (intentional)
# MAGIC
# MAGIC | Scenario | head.VALUE | tender_sum | delta | threshold | Fires? |
# MAGIC |---|---|---|---|---|---|
# MAGIC | clean SALE | 100.00 | 100.00 | 0 | 1.00 | no |
# MAGIC | within 1% | 100.00 | 100.50 | -0.50 | 1.00 | no |
# MAGIC | beyond 1% | 100.00 | 102.00 | -2.00 | 1.00 | **yes** |
# MAGIC | head=0, no tender (PVOID) | 0 | 0 | 0 | 0 | no |
# MAGIC | head=0, stray tender | 0 | 10.00 | -10.00 | 0 | **yes** (suspicious) |
# MAGIC | no tender, head>0 | 100.00 | 0 | 100.00 | 1.00 | **yes** (payment missing) |
# MAGIC | refund | -100.00 | -100.00 | 0 | 1.00 | no |
# MAGIC
# MAGIC ## Expected outcomes by channel
# MAGIC
# MAGIC | Channel | Expected | Reason |
# MAGIC |---|---|---|
# MAGIC | MKT | 0 | conformance synthesised `TENDER_AMT = total_amt` per order |
# MAGIC | POS | tens-to-hundreds | PVOID/RETURN/NOSALE oddities + fault injection |
# MAGIC | OMS | many | head=SUM(price+freight), tender=SUM(payment_value); Olist payment data includes promotional adjustments not reflected in item-level pricing |

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & framework

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, lit, concat, coalesce, when,
    sum as f_sum,
    abs as f_abs,
)
from pyspark.sql.types import DecimalType, StringType

# COMMAND ----------

# MAGIC %run ../_shared/rule_framework

# COMMAND ----------

# MAGIC %md
# MAGIC ## Rule constants

# COMMAND ----------

RULE_ID   = "R03_HEAD_TENDER_TOTAL"
RULE_NAME = "sa_tran_head.VALUE matches SUM(sa_tran_tender.TENDER_AMT) within 1% per transaction"
SEVERITY  = Severity.MINOR

TOLERANCE_PCT = 0.01   # 1% — locked decision A

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
    """abs(head.VALUE - SUM(TENDER_AMT)) > abs(head.VALUE) * 0.01."""
    DECIMAL_TYPE  = DecimalType(20, 4)
    ZERO          = lit(0).cast(DECIMAL_TYPE)
    TOLERANCE_COL = lit(TOLERANCE_PCT).cast(DECIMAL_TYPE)

    head = spark.table("retaildp.silver.sa_tran_head").select(
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
        col("VALUE").cast(DECIMAL_TYPE).alias("_head_value"),
    )

    # LEFT JOIN — some transactions (NOSALE, PVOID) legitimately have no tender rows.
    # COALESCE to 0 makes head>0/tender=0 fire (payment missing → audit-worthy).
    tender_totals = (
        spark.table("retaildp.silver.sa_tran_tender")
        .groupBy("TRAN_SEQ_NO")
        .agg(f_sum("TENDER_AMT").cast(DECIMAL_TYPE).alias("_tender_sum"))
    )

    drift = (
        head
        .join(tender_totals, "TRAN_SEQ_NO", "left")
        .withColumn("_tender_sum", coalesce(col("_tender_sum"), ZERO))
        .withColumn("_delta",     (col("_head_value") - col("_tender_sum")).cast(DECIMAL_TYPE))
        .withColumn("_abs_delta", f_abs(col("_delta")).cast(DECIMAL_TYPE))
        .withColumn("_threshold", (f_abs(col("_head_value")) * TOLERANCE_COL).cast(DECIMAL_TYPE))
        .where(col("_abs_delta") > col("_threshold"))
    )

    narrow = drift.select(
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
        col("_tender_sum").alias("MEASURED_VALUE"),    # what tender computes
        col("_head_value").alias("EXPECTED_VALUE"),    # what head says
        col("_delta").alias("DELTA"),                  # head - tender
        concat(
            lit("head="),         col("_head_value").cast(StringType()),
            lit(", tender_sum="), col("_tender_sum").cast(StringType()),
            lit(", delta="),      col("_delta").cast(StringType()),
            lit(", threshold="),  col("_threshold").cast(StringType()),
            lit(" (1% of |head|)"),
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

print(f"=== Validation: {RULE_ID} ===\n")

total_txns = spark.table("retaildp.silver.sa_tran_head").count()
this_rule  = spark.table(TARGET_TABLE).where(col("RULE_ID") == RULE_ID)
flagged    = this_rule.count()

print(f"Total transactions in sa_tran_head: {total_txns:,}")
print(f"Flagged by {RULE_ID}:                {flagged:,}  ({100.0 * flagged / total_txns:.4f}%)")
print(f"Clean for this rule:                 {total_txns - flagged:,}")

if flagged > 0:
    print("\nFindings by channel:")
    this_rule.groupBy("RTLOG_ORIG_SYS").count().orderBy(col("count").desc()).show()

    # Drift percentage distribution — the OMS story
    print("Drift percentage distribution (|delta| / |head| where head != 0):")
    (
        this_rule
        .where(col("EXPECTED_VALUE") != 0)
        .withColumn(
            "drift_pct",
            (f_abs(col("DELTA")) / f_abs(col("EXPECTED_VALUE")) * 100).cast(DecimalType(10, 2)),
        )
        .withColumn(
            "drift_bucket",
            when(col("drift_pct") <= 2,  lit("1-2%"))
            .when(col("drift_pct") <= 5,  lit("2-5%"))
            .when(col("drift_pct") <= 10, lit("5-10%"))
            .when(col("drift_pct") <= 25, lit("10-25%"))
            .otherwise(lit(">25%"))
        )
        .groupBy("RTLOG_ORIG_SYS", "drift_bucket")
        .count()
        .orderBy("RTLOG_ORIG_SYS", "drift_bucket")
        .show(truncate=False)
    )

    # No-tender findings (head > 0 but no payment recorded)
    no_tender_count = this_rule.where(col("MEASURED_VALUE") == 0).count()
    print(f"Findings with no tender at all (payment missing): {no_tender_count:,}")

    print("\nTop 10 violators (largest |DELTA|):")
    (
        this_rule
        .select(
            "TRAN_SEQ_NO", "RTLOG_ORIG_SYS", "STORE", "BUSINESS_DATE",
            "MEASURED_VALUE", "EXPECTED_VALUE", "DELTA", "ERROR_DESC",
        )
        .orderBy(f_abs(col("DELTA")).desc())
        .show(10, truncate=False)
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## sa_error overview

# COMMAND ----------

if spark.catalog.tableExists(TARGET_TABLE):
    print(f"Total findings across all rules: {spark.table(TARGET_TABLE).count():,}\n")
    spark.table(TARGET_TABLE).groupBy("RULE_ID", "SEVERITY").count().orderBy("RULE_ID").show(truncate=False)
