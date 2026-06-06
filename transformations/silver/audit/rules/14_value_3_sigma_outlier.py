# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R14 — Transaction value within ±3σ of (store, date) distribution
# MAGIC
# MAGIC Statistical outlier detection. For each (STORE, BUSINESS_DATE), compute the
# MAGIC mean and stddev of `sa_tran_head.VALUE`; flag any transaction whose value is
# MAGIC more than 3 standard deviations from the mean. This catches genuinely unusual
# MAGIC transactions — fraud signals, large business orders, mis-keyed prices —
# MAGIC without needing prior knowledge of what "unusual" means for a given store.
# MAGIC
# MAGIC | Property | Value |
# MAGIC |---|---|
# MAGIC | **Rule ID** | `R14_VALUE_3_SIGMA_OUTLIER` |
# MAGIC | **Severity** | `W` (informational — auditor reviews flagged outliers; many are legitimate) |
# MAGIC | **Inputs** | `sa_tran_head` |
# MAGIC | **Method** | window aggregation over (STORE, BUSINESS_DATE) |
# MAGIC | **Scope** | per-transaction |
# MAGIC | **Excludes** | `VALUE = 0` (voided/zeroed transactions handled by R02) |
# MAGIC | **Excludes** | (store, date) groups with fewer than 30 transactions — stddev unreliable below that |

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.window import Window
from pyspark.sql.functions import (
    col, lit, concat, abs as f_abs,
    avg as f_avg,
    stddev as f_stddev,
    count as f_count,
)
from pyspark.sql.types import DecimalType, StringType

# COMMAND ----------

# MAGIC %run ../_shared/rule_framework

# COMMAND ----------

RULE_ID   = "R14_VALUE_3_SIGMA_OUTLIER"
RULE_NAME = "sa_tran_head.VALUE within 3 stddev of mean per (STORE, BUSINESS_DATE)"
SEVERITY  = Severity.WARNING

SIGMA_THRESHOLD = 3.0
MIN_GROUP_SIZE  = 30   # stddev is unreliable for small groups

# COMMAND ----------

if spark.catalog.tableExists(TARGET_TABLE):
    pre_count = spark.table(TARGET_TABLE).where(col("RULE_ID") == RULE_ID).count()
    if pre_count > 0:
        print(f"Deleting {pre_count:,} prior findings for {RULE_ID}.")
        spark.sql(f"DELETE FROM {TARGET_TABLE} WHERE RULE_ID = '{RULE_ID}'")

# COMMAND ----------

def run(spark) -> DataFrame:
    DECIMAL_TYPE = DecimalType(20, 4)
    w            = Window.partitionBy("STORE", "BUSINESS_DATE")

    head_with_stats = (
        spark.table("retaildp.silver.sa_tran_head")
        .where(col("VALUE") != 0)                                            # exclude zeroed
        .withColumn("_group_n",  f_count("*").over(w))
        .withColumn("_mean",     f_avg(col("VALUE")).over(w))
        .withColumn("_stddev",   f_stddev(col("VALUE")).over(w))
        .where(col("_group_n") >= MIN_GROUP_SIZE)
        .where(col("_stddev") > 0)                                           # avoid div-by-zero
        .withColumn(
            "_z_score",
            (f_abs(col("VALUE") - col("_mean")) / col("_stddev")).cast(DecimalType(10, 4))
        )
        .where(col("_z_score") > SIGMA_THRESHOLD)
    )

    narrow = head_with_stats.select(
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
        col("VALUE").cast(DECIMAL_TYPE).alias("MEASURED_VALUE"),
        col("_mean").cast(DECIMAL_TYPE).alias("EXPECTED_VALUE"),
        (col("VALUE") - col("_mean")).cast(DECIMAL_TYPE).alias("DELTA"),
        concat(
            lit("VALUE="),  col("VALUE").cast(StringType()),
            lit(", mean="), col("_mean").cast(DecimalType(20, 2)).cast(StringType()),
            lit(", stddev="), col("_stddev").cast(DecimalType(20, 2)).cast(StringType()),
            lit(", z="), col("_z_score").cast(StringType()),
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

    print("Top 10 outliers (largest |DELTA|):")
    this_rule.select(
        "TRAN_SEQ_NO", "RTLOG_ORIG_SYS", "STORE", "BUSINESS_DATE",
        "MEASURED_VALUE", "EXPECTED_VALUE", "DELTA", "ERROR_DESC",
    ).orderBy(f_abs(col("DELTA")).desc()).show(10, truncate=False)
