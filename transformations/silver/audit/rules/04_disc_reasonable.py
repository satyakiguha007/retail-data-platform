# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R04 — `abs(SUM(disc))` ≤ `abs(SUM(items))` per transaction
# MAGIC
# MAGIC Discount sanity check. Total discount on a transaction should never exceed
# MAGIC the total gross item value. 100% off is legitimate (BOGO, comp, freebie);
# MAGIC strictly more than 100% off is an audit signal — either over-applied promo,
# MAGIC coupon stacking gone wrong, or data corruption.
# MAGIC
# MAGIC | Property | Value |
# MAGIC |---|---|
# MAGIC | **Rule ID** | `R04_DISC_REASONABLE` |
# MAGIC | **Severity** | `W` (warning — 100% legitimate; >100% audit-worthy) |
# MAGIC | **Inputs** | `sa_tran_head`, `sa_tran_item`, `sa_tran_disc` |
# MAGIC | **Verified columns** | `item.{QTY, UNIT_RETAIL}` · `disc.{QTY, UNIT_DISCOUNT_AMT}` |
# MAGIC | **Scope** | TRAN-level (consistent with framework PK) |
# MAGIC
# MAGIC ## Why INNER JOIN on `sa_tran_disc`
# MAGIC
# MAGIC Transactions without any discount row have nothing to over-discount. INNER JOIN
# MAGIC drops those automatically. Olist (no disc rows in entire channel) → excluded.
# MAGIC POS/MKT transactions with no disc → excluded. We only audit transactions where
# MAGIC at least one discount was applied.
# MAGIC
# MAGIC ## Tolerance — hard equality
# MAGIC
# MAGIC `abs(disc) > abs(item)` strictly. `abs(disc) == abs(item)` (100% off) is allowed.
# MAGIC No tolerance band — at this scope, decimal precision artefacts are bounded by
# MAGIC the column's DecimalType(20,4) arithmetic.
# MAGIC
# MAGIC ## Expected outcomes per channel
# MAGIC
# MAGIC | Channel | Expected | Reason |
# MAGIC |---|---|---|
# MAGIC | OMS | 0 | no disc rows → excluded by INNER JOIN |
# MAGIC | MKT | 0 | synthesised 40% discount, well below 100% ceiling |
# MAGIC | POS | depends on fault injection | only fires if simulator injects extreme discounts |

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & framework

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, lit, concat, when,
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

RULE_ID   = "R04_DISC_REASONABLE"
RULE_NAME = "abs(SUM(disc.QTY * UNIT_DISCOUNT_AMT)) <= abs(SUM(item.QTY * UNIT_RETAIL)) per transaction"
SEVERITY  = Severity.WARNING

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
    """abs(SUM(disc.QTY * UNIT_DISCOUNT_AMT)) > abs(SUM(item.QTY * UNIT_RETAIL))."""
    DECIMAL_TYPE = DecimalType(20, 4)
    ZERO         = lit(0).cast(DECIMAL_TYPE)

    head = spark.table("retaildp.silver.sa_tran_head").select(
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
    )

    item_totals = (
        spark.table("retaildp.silver.sa_tran_item")
        .selectExpr(
            "TRAN_SEQ_NO",
            "CAST(QTY * UNIT_RETAIL AS DECIMAL(20, 4)) AS _line_gross",
        )
        .groupBy("TRAN_SEQ_NO")
        .agg(f_sum("_line_gross").cast(DECIMAL_TYPE).alias("_item_gross"))
    )

    disc_totals = (
        spark.table("retaildp.silver.sa_tran_disc")
        .selectExpr(
            "TRAN_SEQ_NO",
            "CAST(QTY * UNIT_DISCOUNT_AMT AS DECIMAL(20, 4)) AS _line_disc",
        )
        .groupBy("TRAN_SEQ_NO")
        .agg(f_sum("_line_disc").cast(DECIMAL_TYPE).alias("_disc_total"))
    )

    violations = (
        head
        .join(item_totals, "TRAN_SEQ_NO", "inner")
        .join(disc_totals, "TRAN_SEQ_NO", "inner")   # INNER — only transactions WITH discounts
        .withColumn("_abs_item", f_abs(col("_item_gross")).cast(DECIMAL_TYPE))
        .withColumn("_abs_disc", f_abs(col("_disc_total")).cast(DECIMAL_TYPE))
        .withColumn("_excess",   (col("_abs_disc") - col("_abs_item")).cast(DECIMAL_TYPE))
        .where(col("_excess") > ZERO)
    )

    narrow = violations.select(
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
        col("_abs_disc").alias("MEASURED_VALUE"),   # actual total discount (absolute)
        col("_abs_item").alias("EXPECTED_VALUE"),   # max allowed (absolute item gross)
        col("_excess").alias("DELTA"),              # how much over (positive)
        concat(
            lit("|disc|="),    col("_abs_disc").cast(StringType()),
            lit(", |items|="), col("_abs_item").cast(StringType()),
            lit(", excess="),  col("_excess").cast(StringType()),
            lit(" ("),
            ((col("_abs_disc") / col("_abs_item")) * 100).cast(DecimalType(10, 2)).cast(StringType()),
            lit("% of gross)"),
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

# Universe — transactions with at least one disc row (the rule's input set)
discounted_txns = spark.table("retaildp.silver.sa_tran_disc").select("TRAN_SEQ_NO").distinct().count()

print(f"Total transactions in sa_tran_head:       {total_txns:,}")
print(f"Transactions with at least one discount:  {discounted_txns:,}")
print(f"Flagged by {RULE_ID}:                      {flagged:,}")
if discounted_txns > 0:
    print(f"  → {100.0 * flagged / discounted_txns:.4f}% of discounted transactions")

if flagged == 0:
    print("\nRule passed cleanly — no transaction has discount exceeding gross item value.")
    print("Simulator-generated discounts are within sanity bounds (≤ 100% of gross).")
else:
    print("\nFindings by channel:")
    this_rule.groupBy("RTLOG_ORIG_SYS").count().orderBy(col("count").desc()).show()

    # Excess severity distribution
    print("Over-discount severity (excess as % of item gross):")
    (
        this_rule
        .where(col("EXPECTED_VALUE") > 0)
        .withColumn(
            "excess_pct",
            ((col("DELTA") / col("EXPECTED_VALUE")) * 100).cast(DecimalType(10, 2)),
        )
        .withColumn(
            "excess_bucket",
            when(col("excess_pct") <= 5,   lit("0-5% over"))
            .when(col("excess_pct") <= 25,  lit("5-25% over"))
            .when(col("excess_pct") <= 100, lit("25-100% over"))
            .otherwise(lit(">100% over"))
        )
        .groupBy("RTLOG_ORIG_SYS", "excess_bucket")
        .count()
        .orderBy("RTLOG_ORIG_SYS", "excess_bucket")
        .show(truncate=False)
    )

    print("Top 10 violators (largest DELTA):")
    (
        this_rule
        .select(
            "TRAN_SEQ_NO", "RTLOG_ORIG_SYS", "STORE", "BUSINESS_DATE",
            "MEASURED_VALUE", "EXPECTED_VALUE", "DELTA", "ERROR_DESC",
        )
        .orderBy(col("DELTA").desc())
        .show(10, truncate=False)
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## sa_error overview

# COMMAND ----------

if spark.catalog.tableExists(TARGET_TABLE):
    print(f"Total findings across all rules: {spark.table(TARGET_TABLE).count():,}\n")
    spark.table(TARGET_TABLE).groupBy("RULE_ID", "SEVERITY").count().orderBy("RULE_ID").show(truncate=False)
