# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R17 — `TRAN_DATETIME` within `BUSINESS_DATE ± 1 day`
# MAGIC
# MAGIC Temporal consistency check. The transaction timestamp's date component must
# MAGIC fall within one day of the assigned `BUSINESS_DATE`. Tolerance ±1 day handles
# MAGIC late-night sales that cross midnight (transacted at 23:58 on day N, settled
# MAGIC into business day N+1). Anything further apart is suspicious — timezone drift,
# MAGIC bad RTLOG mapping, or fault injection.
# MAGIC
# MAGIC | Property | Value |
# MAGIC |---|---|
# MAGIC | **Rule ID** | `R17_DATETIME_BUSINESS_DATE_ALIGNED` |
# MAGIC | **Severity** | `M` (minor) |
# MAGIC | **Inputs** | `sa_tran_head` only |
# MAGIC | **Tolerance** | channel-aware: POS/MKT ±1 day; OMS ±14 days (Olist payment-approval lag is normal) |
# MAGIC | **Scope** | per-transaction |

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, lit, concat, abs as f_abs, when,
    to_date, datediff,
)
from pyspark.sql.types import DecimalType, StringType

# COMMAND ----------

# MAGIC %run ../_shared/rule_framework

# COMMAND ----------

RULE_ID   = "R17_DATETIME_BUSINESS_DATE_ALIGNED"
RULE_NAME = "abs(datediff(date(TRAN_DATETIME), BUSINESS_DATE)) <= channel_tolerance"
SEVERITY  = Severity.MINOR

# Channel-aware tolerances. OMS payment-approval lag is normal in Olist —
# customer purchases on day N, payment clears on N+2 to N+10 routinely.
# POS/MKT are real-time so should align tightly.
MAX_DIFF_BY_CHANNEL = {
    "POS": 1,
    "MKT": 1,
    "OMS": 14,
}

# COMMAND ----------

if spark.catalog.tableExists(TARGET_TABLE):
    pre_count = spark.table(TARGET_TABLE).where(col("RULE_ID") == RULE_ID).count()
    if pre_count > 0:
        print(f"Deleting {pre_count:,} prior findings for {RULE_ID}.")
        spark.sql(f"DELETE FROM {TARGET_TABLE} WHERE RULE_ID = '{RULE_ID}'")

# COMMAND ----------

def run(spark) -> DataFrame:
    DECIMAL_TYPE = DecimalType(20, 4)

    head = (
        spark.table("retaildp.silver.sa_tran_head")
        .withColumn("_tran_date",   to_date(col("TRAN_DATETIME")))
        .withColumn("_diff_days",   datediff(col("_tran_date"), col("BUSINESS_DATE")))
        .withColumn("_abs_diff",    f_abs(col("_diff_days")))
        .withColumn(
            "_max_allowed",
            when(col("RTLOG_ORIG_SYS") == "OMS", lit(MAX_DIFF_BY_CHANNEL["OMS"]))
            .when(col("RTLOG_ORIG_SYS") == "MKT", lit(MAX_DIFF_BY_CHANNEL["MKT"]))
            .otherwise(lit(MAX_DIFF_BY_CHANNEL["POS"]))
        )
        .where(col("_abs_diff") > col("_max_allowed"))
    )

    narrow = head.select(
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
        col("_abs_diff").cast(DECIMAL_TYPE).alias("MEASURED_VALUE"),
        col("_max_allowed").cast(DECIMAL_TYPE).alias("EXPECTED_VALUE"),
        (col("_abs_diff") - col("_max_allowed")).cast(DECIMAL_TYPE).alias("DELTA"),
        concat(
            lit("TRAN_DATETIME="),    col("TRAN_DATETIME").cast(StringType()),
            lit(", BUSINESS_DATE="),  col("BUSINESS_DATE").cast(StringType()),
            lit(", diff_days="),      col("_diff_days").cast(StringType()),
            lit(", channel_max="),    col("_max_allowed").cast(StringType()),
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
    print("Rule passed cleanly — every TRAN_DATETIME aligns with its BUSINESS_DATE (±1 day).")
else:
    print("\nFindings by channel:")
    this_rule.groupBy("RTLOG_ORIG_SYS").count().orderBy(col("count").desc()).show()
    print("Sample findings:")
    this_rule.select("TRAN_SEQ_NO", "RTLOG_ORIG_SYS", "ERROR_DESC").limit(10).show(truncate=False)
