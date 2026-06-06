# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R18 — `head.CURRENCY_CODE` matches `sa_store_data.CURRENCY_CODE`
# MAGIC
# MAGIC Dimensional consistency check. The transaction's currency must equal the store
# MAGIC dimension's declared currency. Mismatches indicate broken FX inheritance during
# MAGIC conformance, or a store record drifting out of sync with transactional data.
# MAGIC
# MAGIC | Property | Value |
# MAGIC |---|---|
# MAGIC | **Rule ID** | `R18_CURRENCY_MATCHES_STORE` |
# MAGIC | **Severity** | `F` (fatal — wrong currency corrupts every downstream USD figure) |
# MAGIC | **Inputs** | `sa_tran_head`, `sa_store_data` |
# MAGIC | **Scope** | per-transaction |
# MAGIC | **Expected result** | `0` — conformance derives `CURRENCY_CODE` from `sa_store_data`; this rule asserts that lineage held |

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import col, lit, concat, when
from pyspark.sql.types import DecimalType, StringType

# COMMAND ----------

# MAGIC %run ../_shared/rule_framework

# COMMAND ----------

RULE_ID   = "R18_CURRENCY_MATCHES_STORE"
RULE_NAME = "sa_tran_head.CURRENCY_CODE matches sa_store_data.CURRENCY_CODE for that STORE"
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

    head = spark.table("retaildp.silver.sa_tran_head").select(
        "TRAN_SEQ_NO", "STORE", "BUSINESS_DATE", "RTLOG_ORIG_SYS",
        col("CURRENCY_CODE").alias("_head_currency"),
    )

    store = spark.table("retaildp.silver.sa_store_data").select(
        "STORE",
        col("CURRENCY_CODE").alias("_store_currency"),
    )

    violations = (
        head.join(store, "STORE", "left")
        .where(
            col("_store_currency").isNull()                            # store not in dim
            | (col("_head_currency") != col("_store_currency"))        # currency mismatch
        )
    )

    narrow = violations.select(
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
        lit(None).cast(DECIMAL_TYPE).alias("MEASURED_VALUE"),
        lit(None).cast(DECIMAL_TYPE).alias("EXPECTED_VALUE"),
        lit(None).cast(DECIMAL_TYPE).alias("DELTA"),
        concat(
            lit("head_currency="), col("_head_currency"),
            lit(", store_currency="),
            when(col("_store_currency").isNull(), lit("[STORE NOT IN sa_store_data]"))
            .otherwise(col("_store_currency")),
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

if flagged == 0:
    print("Rule passed cleanly — every transaction's currency matches its store dimension.")
else:
    print("\nFindings by channel + currency mismatch pattern:")
    this_rule.groupBy("RTLOG_ORIG_SYS", "ERROR_DESC").count().orderBy(col("count").desc()).show(truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## sa_error overview (run after the final rule of a batch)

# COMMAND ----------

if spark.catalog.tableExists(TARGET_TABLE):
    print(f"Total findings across all rules: {spark.table(TARGET_TABLE).count():,}\n")
    spark.table(TARGET_TABLE).groupBy("RULE_ID", "SEVERITY").count().orderBy("RULE_ID").show(truncate=False)
