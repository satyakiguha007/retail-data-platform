# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R11 — every `sa_tran_head.STORE_DAY_SEQ_NO` exists in `sa_store_day`
# MAGIC
# MAGIC FK integrity assertion. Silver DQ already enforces this at write time (orphans
# MAGIC are quarantined). R11 is the formal audit version — a clean run is a useful
# MAGIC assertion that the DQ contract held end-to-end.
# MAGIC
# MAGIC | Property | Value |
# MAGIC |---|---|
# MAGIC | **Rule ID** | `R11_HEAD_STORE_DAY_FK` |
# MAGIC | **Severity** | `F` |
# MAGIC | **Inputs** | `sa_tran_head`, `sa_store_day` |
# MAGIC | **Pattern** | LEFT ANTI JOIN |
# MAGIC | **Expected result** | `0` |

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import col, lit, concat
from pyspark.sql.types import DecimalType, StringType

# COMMAND ----------

# MAGIC %run ../_shared/rule_framework

# COMMAND ----------

RULE_ID   = "R11_HEAD_STORE_DAY_FK"
RULE_NAME = "Every sa_tran_head.STORE_DAY_SEQ_NO must exist in sa_store_day"
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
        spark.table("retaildp.silver.sa_tran_head").alias("h")
        .join(
            spark.table("retaildp.silver.sa_store_day").select("STORE_DAY_SEQ_NO").alias("d"),
            on="STORE_DAY_SEQ_NO", how="left_anti",
        )
        .select("TRAN_SEQ_NO", "STORE", "BUSINESS_DATE", "RTLOG_ORIG_SYS", "STORE_DAY_SEQ_NO")
    )

    narrow = orphans.select(
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
        lit(None).cast(DECIMAL_TYPE).alias("MEASURED_VALUE"),
        lit(None).cast(DECIMAL_TYPE).alias("EXPECTED_VALUE"),
        lit(None).cast(DECIMAL_TYPE).alias("DELTA"),
        concat(
            lit("orphan head — STORE_DAY_SEQ_NO="),
            col("STORE_DAY_SEQ_NO").cast(StringType()),
            lit(" not present in sa_store_day"),
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
    print("Rule passed cleanly — every sa_tran_head row has a matching sa_store_day parent.")
else:
    spark.table(TARGET_TABLE).where(col("RULE_ID") == RULE_ID) \
        .select("TRAN_SEQ_NO", "RTLOG_ORIG_SYS", "ERROR_DESC").limit(10).show(truncate=False)
