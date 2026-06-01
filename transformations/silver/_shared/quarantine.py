# Databricks notebook source
# MAGIC %md
# MAGIC # `_shared` / `quarantine`
# MAGIC
# MAGIC The idempotent-MERGE + reject-quarantine write pattern, factored out of `01`–`06`.
# MAGIC Every silver micro-batch ends with the same two writes: MERGE clean rows into
# MAGIC the target on the composite PK, append rejects to the quarantine table. The
# MAGIC quarantine table is created on first write — no explicit DDL needed.
# MAGIC
# MAGIC ## Usage
# MAGIC ```python
# MAGIC # MAGIC %run ../_shared/quarantine
# MAGIC
# MAGIC # 01 (single-key PK):
# MAGIC clean_n, reject_n = merge_and_quarantine(
# MAGIC     clean_df, rejects_df, TARGET_TABLE, QUARANTINE_TABLE,
# MAGIC     merge_keys=["TRAN_SEQ_NO"],
# MAGIC )
# MAGIC
# MAGIC # 03 (4-col composite PK):
# MAGIC clean_n, reject_n = merge_and_quarantine(
# MAGIC     clean_df, rejects_df, TARGET_TABLE, QUARANTINE_TABLE,
# MAGIC     merge_keys=["TRAN_SEQ_NO", "ITEM_SEQ_NO", "DISCOUNT_SEQ_NO", "RMS_PROMO_TYPE"],
# MAGIC )
# MAGIC ```
# MAGIC
# MAGIC ## Contract
# MAGIC - `clean_df` must already be projected to the target schema's column order.
# MAGIC - `rejects_df` must already carry the `rejection_reason` column (ArrayType<StringType>).
# MAGIC - `_quarantine_ts = current_timestamp()` is added inside this helper — caller does NOT need to add it.
# MAGIC - MERGE uses `whenMatchedUpdateAll` + `whenNotMatchedInsertAll` — fully idempotent.

# COMMAND ----------

from pyspark.sql import SparkSession, DataFrame
from pyspark.sql.functions import current_timestamp
from delta.tables import DeltaTable


def merge_and_quarantine(
    clean_df: DataFrame,
    rejects_df: DataFrame,
    target_table: str,
    quarantine_table: str,
    merge_keys: list,
) -> tuple:
    """Final write phase of every silver foreachBatch.

    Args:
        clean_df:         DataFrame projected to target schema column order.
        rejects_df:       DataFrame with rejection_reason populated.
        target_table:     fully-qualified silver target.
        quarantine_table: fully-qualified quarantine target (created on first write).
        merge_keys:       ordered list of column names forming the target's PK.

    Returns:
        (clean_n, reject_n) — counts for the per-batch print line.
    """
    if not merge_keys:
        raise ValueError("merge_keys must be a non-empty list")

    spark = SparkSession.getActiveSession()

    # MERGE clean rows on the composite PK
    target = DeltaTable.forName(spark, target_table)
    merge_cond = " AND ".join(f"t.{k} = s.{k}" for k in merge_keys)
    (
        target.alias("t")
        .merge(clean_df.alias("s"), merge_cond)
        .whenMatchedUpdateAll()
        .whenNotMatchedInsertAll()
        .execute()
    )

    # Append rejects (create-on-first-write)
    reject_n = rejects_df.count()
    if reject_n > 0:
        rejects_with_ts = rejects_df.withColumn("_quarantine_ts", current_timestamp())
        if not spark.catalog.tableExists(quarantine_table):
            print(f"Creating {quarantine_table}.")
            rejects_with_ts.write.format("delta").saveAsTable(quarantine_table)
        else:
            rejects_with_ts.write.format("delta").mode("append").saveAsTable(quarantine_table)

    clean_n = clean_df.count()
    return clean_n, reject_n

