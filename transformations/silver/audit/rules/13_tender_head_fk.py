# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R13 — every `sa_tran_tender.TRAN_SEQ_NO` exists in `sa_tran_head`
# MAGIC
# MAGIC FK integrity assertion for tender lines. Silver DQ enforces this — orphans
# MAGIC quarantined. R13 is the formal audit version.
# MAGIC
# MAGIC | Property | Value |
# MAGIC |---|---|
# MAGIC | **Rule ID** | `R13_TENDER_HEAD_FK` |
# MAGIC | **Severity** | `F` |
# MAGIC | **Inputs** | `sa_tran_tender`, `sa_tran_head` |
# MAGIC | **Pattern** | LEFT ANTI JOIN, aggregated to TRAN_SEQ_NO |
# MAGIC | **Expected result** | `0` |

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import col, lit, concat, count as f_count
from pyspark.sql.types import DecimalType, StringType

# COMMAND ----------

# MAGIC %run ../_shared/rule_framework

# COMMAND ----------

RULE_ID   = "R13_TENDER_HEAD_FK"
RULE_NAME = "Every sa_tran_tender.TRAN_SEQ_NO must exist in sa_tran_head"
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

    orphans = (
        spark.table("retaildp.silver.sa_tran_tender").alias("t")
        .join(
            spark.table("retaildp.silver.sa_tran_head").select("TRAN_SEQ_NO").alias("h"),
            on="TRAN_SEQ_NO", how="left_anti",
        )
        .groupBy("TRAN_SEQ_NO", "STORE", "BUSINESS_DATE", "RTLOG_ORIG_SYS")
        .agg(f_count("*").alias("_orphan_lines"))
    )

    narrow = orphans.select(
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
        col("_orphan_lines").cast(DECIMAL_TYPE).alias("MEASURED_VALUE"),
        lit(0).cast(DECIMAL_TYPE).alias("EXPECTED_VALUE"),
        col("_orphan_lines").cast(DECIMAL_TYPE).alias("DELTA"),
        concat(
            lit("orphan tender lines with no parent header: "),
            col("_orphan_lines").cast(StringType()),
        ).alias("ERROR_DESC"),
    )

    return emit_findings(narrow, RULE_ID, RULE_NAME, SEVERITY)

# COMMAND ----------

findings = run(spark)
n_written = write_findings(findings)
print(f"\n{RULE_ID}: {n_written} finding(s) written to {TARGET_TABLE}")

# COMMAND ----------

print(f"=== Validation: {RULE_ID} ===\n")
flagged = spark.table(TARGET_TABLE).where(col("RULE_ID") == RULE_ID).count()
print(f"Flagged: {flagged:,}")

if flagged == 0:
    print("Rule passed cleanly — every tender line has a matching head.")
else:
    spark.table(TARGET_TABLE).where(col("RULE_ID") == RULE_ID) \
        .select("TRAN_SEQ_NO", "RTLOG_ORIG_SYS", "ERROR_DESC").limit(10).show(truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## sa_error overview — after batch 2 of 3

# COMMAND ----------

if spark.catalog.tableExists(TARGET_TABLE):
    print(f"Total findings across all rules: {spark.table(TARGET_TABLE).count():,}\n")
    spark.table(TARGET_TABLE).groupBy("RULE_ID", "SEVERITY").count().orderBy("RULE_ID").show(truncate=False)
