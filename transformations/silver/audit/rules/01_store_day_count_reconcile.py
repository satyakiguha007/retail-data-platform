# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R01 — `sa_store_day.RTLOG_RECORD_COUNT` matches `COUNT(POS sa_tran_head)`
# MAGIC
# MAGIC Store-day reconciliation: the bronze RTLOG record count tracked on `sa_store_day`
# MAGIC should equal the number of POS transactions that landed in silver. Mismatches
# MAGIC indicate quarantined POS heads (DQ failures) — auditor should know how many
# MAGIC transactions failed conformance per (store, date).
# MAGIC
# MAGIC | Property | Value |
# MAGIC |---|---|
# MAGIC | **Rule ID** | `R01_STORE_DAY_COUNT_RECONCILE` |
# MAGIC | **Severity** | `M` |
# MAGIC | **Inputs** | `sa_store_day`, `sa_tran_head` (POS only) |
# MAGIC | **Scope** | per (store, date); finding key = `STORE_DAY_SEQ_NO` (used in TRAN_SEQ_NO slot) |
# MAGIC | **Why POS-only** | `RTLOG_RECORD_COUNT` is POS-specific; MKT/OMS sa_store_day rows are insert-if-not-exists, don't track this count |

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import col, lit, concat, count as f_count
from pyspark.sql.types import DecimalType, StringType

# COMMAND ----------

# MAGIC %run ../_shared/rule_framework

# COMMAND ----------

RULE_ID   = "R01_STORE_DAY_COUNT_RECONCILE"
RULE_NAME = "sa_store_day.RTLOG_RECORD_COUNT matches COUNT(sa_tran_head where RTLOG_ORIG_SYS='POS') per (STORE, BUSINESS_DATE)"
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

    pos_head_counts = (
        spark.table("retaildp.silver.sa_tran_head")
        .where(col("RTLOG_ORIG_SYS") == "POS")
        .groupBy("STORE", "BUSINESS_DATE")
        .agg(f_count("*").cast(DECIMAL_TYPE).alias("_silver_count"))
    )

    store_day = spark.table("retaildp.silver.sa_store_day").select(
        "STORE_DAY_SEQ_NO", "STORE", "BUSINESS_DATE",
        col("RTLOG_RECORD_COUNT").cast(DECIMAL_TYPE).alias("_bronze_count"),
    )

    violations = (
        store_day.join(pos_head_counts, ["STORE", "BUSINESS_DATE"], "inner")
        .where(col("_silver_count") != col("_bronze_count"))
    )

    narrow = violations.select(
        col("STORE_DAY_SEQ_NO").alias("TRAN_SEQ_NO"),    # pseudo — store-day hash
        col("STORE"),
        col("BUSINESS_DATE"),
        lit("POS").alias("RTLOG_ORIG_SYS"),
        col("_silver_count").alias("MEASURED_VALUE"),
        col("_bronze_count").alias("EXPECTED_VALUE"),
        (col("_silver_count") - col("_bronze_count")).alias("DELTA"),
        concat(
            lit("POS heads in silver="), col("_silver_count").cast(StringType()),
            lit(", RTLOG_RECORD_COUNT="), col("_bronze_count").cast(StringType()),
            lit(", drift="), (col("_silver_count") - col("_bronze_count")).cast(StringType()),
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
print(f"Flagged (store, date) pairs: {flagged:,}")
if flagged > 0:
    spark.table(TARGET_TABLE).where(col("RULE_ID") == RULE_ID) \
        .select("STORE", "BUSINESS_DATE", "MEASURED_VALUE", "EXPECTED_VALUE", "DELTA", "ERROR_DESC") \
        .show(20, truncate=False)
else:
    print("Rule passed — every POS (store, date) reconciles bronze count to silver count.")
