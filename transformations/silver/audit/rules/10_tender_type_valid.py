# Databricks notebook source
# MAGIC %md
# MAGIC # Audit Rule R10 — `sa_tran_tender.TENDER_TYPE_GROUP` in canonical valid set
# MAGIC
# MAGIC Whitelist check. ReSA expects `TENDER_TYPE_GROUP` to come from a controlled
# MAGIC vocabulary. Values outside this set indicate either an unmapped source code
# MAGIC (silver conformance bug — a new payment method appeared that the channel writer
# MAGIC didn't normalise) or fault-injected garbage.
# MAGIC
# MAGIC | Property | Value |
# MAGIC |---|---|
# MAGIC | **Rule ID** | `R10_TENDER_TYPE_VALID` |
# MAGIC | **Severity** | `F` |
# MAGIC | **Inputs** | `sa_tran_tender`, `sa_tran_head` (for context) |
# MAGIC | **Scope** | TRAN-level (one finding per transaction with at least one bad tender line) |
# MAGIC
# MAGIC ## Valid set (channel-union)
# MAGIC
# MAGIC | Channel | Values |
# MAGIC |---|---|
# MAGIC | POS standard | CARD, CASH, CHECK, COUPON, EBT, GIFT_CARD, STORED_VALUE |
# MAGIC | Card subtypes | CREDIT, DEBIT |
# MAGIC | Olist | VOUCHER, BOLETO, UNDEFINED |
# MAGIC | MKT | MARKETPLACE (sentinel — channel writer synthesizes single tender row per order) |
# MAGIC | Misc | HOUSE_ACCT, FOREIGN_CURR |
# MAGIC
# MAGIC If this rule fires on a value that's actually legitimate for our data, the fix
# MAGIC is to add it to `VALID_TENDER_TYPE_GROUPS` below — not to suppress the rule.

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import col, lit, concat, count as f_count, collect_set, concat_ws
from pyspark.sql.types import DecimalType, StringType

# COMMAND ----------

# MAGIC %run ../_shared/rule_framework

# COMMAND ----------

RULE_ID   = "R10_TENDER_TYPE_VALID"
RULE_NAME = "sa_tran_tender.TENDER_TYPE_GROUP must be in the canonical valid set"
SEVERITY  = Severity.FATAL

VALID_TENDER_TYPE_GROUPS = [
    # POS standard
    "CARD", "CASH", "CHECK", "COUPON", "EBT", "GIFT_CARD", "STORED_VALUE",
    # Card subtypes
    "CREDIT", "DEBIT",
    # Olist
    "VOUCHER", "BOLETO", "UNDEFINED",
    # MKT (synthesized — single sentinel value for marketplace channel)
    "MARKETPLACE",
    # Misc
    "HOUSE_ACCT", "FOREIGN_CURR",
]

# COMMAND ----------

if spark.catalog.tableExists(TARGET_TABLE):
    pre_count = spark.table(TARGET_TABLE).where(col("RULE_ID") == RULE_ID).count()
    if pre_count > 0:
        print(f"Deleting {pre_count:,} prior findings for {RULE_ID}.")
        spark.sql(f"DELETE FROM {TARGET_TABLE} WHERE RULE_ID = '{RULE_ID}'")

# COMMAND ----------

def run(spark) -> DataFrame:
    DECIMAL_TYPE = DecimalType(20, 4)

    bad_tenders = (
        spark.table("retaildp.silver.sa_tran_tender")
        .where(~col("TENDER_TYPE_GROUP").isin(VALID_TENDER_TYPE_GROUPS))
        .select("TRAN_SEQ_NO", "TENDER_SEQ_NO", "TENDER_TYPE_GROUP")
    )

    head = spark.table("retaildp.silver.sa_tran_head").select(
        "TRAN_SEQ_NO", "STORE", "BUSINESS_DATE", "RTLOG_ORIG_SYS",
    )

    violations = (
        bad_tenders.join(head, "TRAN_SEQ_NO", "inner")
        .groupBy("TRAN_SEQ_NO", "STORE", "BUSINESS_DATE", "RTLOG_ORIG_SYS")
        .agg(
            f_count("*").alias("_bad_count"),
            collect_set("TENDER_TYPE_GROUP").alias("_bad_values"),
        )
    )

    narrow = violations.select(
        col("TRAN_SEQ_NO"),
        col("STORE"),
        col("BUSINESS_DATE"),
        col("RTLOG_ORIG_SYS"),
        col("_bad_count").cast(DECIMAL_TYPE).alias("MEASURED_VALUE"),
        lit(0).cast(DECIMAL_TYPE).alias("EXPECTED_VALUE"),
        col("_bad_count").cast(DECIMAL_TYPE).alias("DELTA"),
        concat(
            lit("Invalid TENDER_TYPE_GROUP values: ["),
            concat_ws(", ", col("_bad_values")),
            lit("], on "), col("_bad_count").cast(StringType()),
            lit(" tender line(s)"),
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

# Diagnostic: distinct TENDER_TYPE_GROUP values present in silver
print("\nAll TENDER_TYPE_GROUP values present in sa_tran_tender:")
(
    spark.table("retaildp.silver.sa_tran_tender")
    .groupBy("TENDER_TYPE_GROUP")
    .count()
    .orderBy(col("count").desc())
    .show(truncate=False)
)

if flagged > 0:
    print("Findings by channel:")
    this_rule.groupBy("RTLOG_ORIG_SYS").count().show()
    print("Sample findings:")
    this_rule.select("TRAN_SEQ_NO", "RTLOG_ORIG_SYS", "ERROR_DESC").limit(10).show(truncate=False)
