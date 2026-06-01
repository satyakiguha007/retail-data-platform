# Databricks notebook source
# MAGIC %md
# MAGIC # `_shared` / `fx_helpers`
# MAGIC
# MAGIC The "FK-enrich-with-parent-FX" pattern factored out of `02`–`06`. Every child
# MAGIC silver table validates its FK against a Silver parent AND inherits
# MAGIC `CURRENCY_CODE` + `FX_RATE` in the same broadcast join. Centralised so the
# MAGIC inheritance contract is consistent across every child — no risk of one child
# MAGIC casting `FX_RATE` to a different precision or aliasing the helper columns
# MAGIC differently.
# MAGIC
# MAGIC ## Usage
# MAGIC ```python
# MAGIC # MAGIC %run ../_shared/fx_helpers
# MAGIC
# MAGIC # Tran-level child (02, 04, 05):
# MAGIC enriched = enrich_with_parent_fx(keyed, PARENT_TABLE, ["TRAN_SEQ_NO"])
# MAGIC
# MAGIC # Line-level child (03, 06):
# MAGIC enriched = enrich_with_parent_fx(keyed, PARENT_TABLE, ["TRAN_SEQ_NO", "ITEM_SEQ_NO"])
# MAGIC ```
# MAGIC
# MAGIC ## Contract
# MAGIC - The DataFrame passed in must already have `join_keys` populated with the same names as on the parent.
# MAGIC - The parent table MUST expose columns named `CURRENCY_CODE` and `FX_RATE` (every silver parent does — `01` produces them, `02` re-exposes them inherited).
# MAGIC - Helper returns the DataFrame with `CURRENCY_CODE`, `FX_RATE`, and `_has_parent` (boolean) added. Temporary `_p_*` join columns are dropped.
# MAGIC - Caller uses `_has_parent` to build the `rejection_reason` array, then drops it before projecting to target schema.

# COMMAND ----------

from functools import reduce
from pyspark.sql import SparkSession, DataFrame
from pyspark.sql.functions import col, broadcast


def enrich_with_parent_fx(df: DataFrame, parent_table: str, join_keys: list) -> DataFrame:
    """Broadcast-join `parent_table` on `join_keys`, inherit CURRENCY_CODE + FX_RATE,
    flag orphans as `_has_parent` (boolean).

    Args:
        df:           the keyed silver DataFrame with `join_keys` already populated.
        parent_table: fully-qualified parent table name (e.g. "retaildp.silver.sa_tran_head").
        join_keys:    ordered list of column names that exist on both sides.

    Returns:
        DataFrame with CURRENCY_CODE, FX_RATE, _has_parent added; _p_* temps dropped.
    """
    if not join_keys:
        raise ValueError("join_keys must be a non-empty list")

    spark = SparkSession.getActiveSession()
    parent = spark.table(parent_table)

    # Alias join columns and FX columns with a _p_ prefix to avoid name clashes on the join
    parent_aliased = parent.select(
        *[col(k).alias(f"_p_{k}") for k in join_keys],
        col("CURRENCY_CODE").alias("_p_CURRENCY_CODE"),
        col("FX_RATE").alias("_p_FX_RATE"),
    )

    # Build the join condition as a conjunction of equalities
    join_cond = reduce(
        lambda a, b: a & b,
        [col(k) == col(f"_p_{k}") for k in join_keys],
    )

    return (
        df.join(broadcast(parent_aliased), join_cond, "left")
        .withColumn("CURRENCY_CODE", col("_p_CURRENCY_CODE"))
        .withColumn("FX_RATE",       col("_p_FX_RATE"))
        # First join key's parent-side nullness == orphan flag
        .withColumn("_has_parent",   col(f"_p_{join_keys[0]}").isNotNull())
        .drop(
            *[f"_p_{k}" for k in join_keys],
            "_p_CURRENCY_CODE", "_p_FX_RATE",
        )
    )

