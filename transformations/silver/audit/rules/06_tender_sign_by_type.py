# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R06 — `SUM(TENDER_AMT)` sign matches `TRAN_TYPE` convention
# MAGIC
# MAGIC Sign mirror of R05, on the tender side. For SALE-class transactions, the
# MAGIC tender total must be positive (customer paid). For RETURN/CREFUND, it must be
# MAGIC negative (refund issued). Sign mismatches indicate a fault — refund logged as
# MAGIC SALE, or a sale recorded with a refund-direction payment.
# MAGIC
# MAGIC | Property | Value |
# MAGIC |---|---|
# MAGIC | **Rule ID** | `R06_TENDER_SIGN_BY_TYPE` |
# MAGIC | **Severity** | `F` |
# MAGIC | **Inputs** | `sa_tran_head` (POS only), `sa_tran_tender` |
# MAGIC | **Tran types checked** | SALE / RETURN / CREFUND (others — PVOID, NOSALE — excluded as zero-sum or no-tender) |
# MAGIC | **Channel scope** | POS only. MKT/OMS use absolute tender values; direction is encoded in `TRAN_TYPE`, not tender sign. |

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import col, lit, concat, when, sum as f_sum
from pyspark.sql.types import DecimalType, StringType

# COMMAND ----------

# MAGIC %run ../_shared/rule_framework

# COMMAND ----------

RULE_ID   = "R06_TENDER_SIGN_BY_TYPE"
RULE_NAME = "SUM(sa_tran_tender.TENDER_AMT) sign matches sa_tran_head.TRAN_TYPE: SALE > 0, RETURN/CREFUND < 0"
SEVERITY  = Severity.FATAL

POSITIVE_TYPES = ["SALE"]
NEGATIVE_TYPES = ["RETURN", "CREFUND"]

# COMMAND ----------

if spark.catalog.tableExists(TARGET_TABLE):
    pre_count = spark.table(TARGET_TABLE).where(col("RULE_ID") == RULE_ID).count()
    if pre_count > 0:
        print(f"Deleting {pre_count:,} prior findings for {RULE_ID}.")
        spark.sql(f"DELETE FROM {TARGET_TABLE} WHERE RULE_ID = '{RULE_ID}'")

# COMMAND ----------

def run(spark) -> DataFrame:
    DECIMAL_TYPE = DecimalType(20, 4)

    head = spark.table("retaildp.silver.sa_tran_head").select(
        "TRAN_SEQ_NO", "STORE", "BUSINESS_DATE", "RTLOG_ORIG_SYS", "TRAN_TYPE",
    ).where(col("RTLOG_ORIG_SYS") == "POS") \
     .where(col("TRAN_TYPE").isin(POSITIVE_TYPES + NEGATIVE_TYPES))

    tender_totals = (
        spark.table("retaildp.silver.sa_tran_tender")
        .groupBy("TRAN_SEQ_NO")
        .agg(f_sum("TENDER_AMT").cast(DECIMAL_TYPE).alias("_tender_sum"))
    )

    # INNER JOIN — only check transactions that have tender rows. No-tender cases
    # are handled by R03 (head ≈ SUM(tender)) and R13 (tender FK), not here.
    violations = (
        head.join(tender_totals, "TRAN_SEQ_NO", "inner")
        .withColumn(
            "_expected_sign",
            when(col("TRAN_TYPE").isin(POSITIVE_TYPES), lit("positive"))
            .otherwise(lit("negative")),
        )
        .withColumn(
            "_actual_sign",
            when(col("_tender_sum") > 0,  lit("positive"))
            .when(col("_tender_sum") < 0, lit("negative"))
            .otherwise(lit("zero")),
        )
        .where(col("_expected_sign") != col("_actual_sign"))
    )

    narrow = violations.select(
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
        col("_tender_sum").alias("MEASURED_VALUE"),
        lit(0).cast(DECIMAL_TYPE).alias("EXPECTED_VALUE"),
        col("_tender_sum").alias("DELTA"),
        concat(
            lit("TRAN_TYPE="),    col("TRAN_TYPE"),
            lit(", expected="),   col("_expected_sign"),
            lit(", actual="),     col("_actual_sign"),
            lit(", tender_sum="), col("_tender_sum").cast(StringType()),
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
