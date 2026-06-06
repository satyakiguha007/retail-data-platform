# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R09 — `head.VENDOR_NO` required for `RTLOG_ORIG_SYS = 'OMS'`
# MAGIC
# MAGIC Channel-specific mandatory check. Refined from the original spec ("non-POS")
# MAGIC after reviewing actual conformance code:
# MAGIC
# MAGIC | Channel | VENDOR_NO populated? | Where seller info lives |
# MAGIC |---|---|---|
# MAGIC | POS | No (not a vendor concept for store sales) | n/a |
# MAGIC | MKT | No — explicitly NULL'd in conformance | `REF_NO1` (marketplace ID) |
# MAGIC | OMS | **Yes — required** | primary seller_id |
# MAGIC
# MAGIC | Property | Value |
# MAGIC |---|---|
# MAGIC | **Rule ID** | `R09_VENDOR_REQUIRED_OMS` |
# MAGIC | **Severity** | `M` (minor) |
# MAGIC | **Inputs** | `sa_tran_head` only |
# MAGIC | **Scope** | per-transaction |
# MAGIC | **Expected result** | `0` — silver DQ enforces it for OMS at write time |

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import col, lit, concat, trim
from pyspark.sql.types import DecimalType, StringType

# COMMAND ----------

# MAGIC %run ../_shared/rule_framework

# COMMAND ----------

RULE_ID   = "R09_VENDOR_REQUIRED_OMS"
RULE_NAME = "sa_tran_head.VENDOR_NO must be populated for RTLOG_ORIG_SYS = 'OMS'"
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

    violations = (
        spark.table("retaildp.silver.sa_tran_head")
        .where(col("RTLOG_ORIG_SYS") == "OMS")
        .where(col("VENDOR_NO").isNull() | (trim(col("VENDOR_NO")) == ""))
        .select("TRAN_SEQ_NO", "STORE", "BUSINESS_DATE", "RTLOG_ORIG_SYS", "TRAN_TYPE")
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
            lit("VENDOR_NO null for OMS transaction (TRAN_TYPE="),
            col("TRAN_TYPE"),
            lit(")"),
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
oms_total = spark.table("retaildp.silver.sa_tran_head").where(col("RTLOG_ORIG_SYS") == "OMS").count()
print(f"OMS transactions in scope: {oms_total:,}")
print(f"Flagged:                    {flagged:,}")

if flagged == 0:
    print("\nRule passed cleanly — every OMS transaction has a populated VENDOR_NO (DQ enforced).")
else:
    this_rule.select("TRAN_SEQ_NO", "ERROR_DESC").limit(10).show(truncate=False)
