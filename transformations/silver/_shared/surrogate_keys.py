# Databricks notebook source
# MAGIC %md
# MAGIC # `_shared` / `surrogate_keys`
# MAGIC
# MAGIC Single source of truth for silver surrogate keys. Imported via `%run` from any
# MAGIC silver notebook. The `TRAN_SEQ_NO` formula must be identical across every source
# MAGIC (POS / Marketplace / Olist) and every child table (`sa_tran_head` and `sa_tran_*`).
# MAGIC Drift here causes silent FK failures across the whole silver layer.
# MAGIC
# MAGIC ## Usage
# MAGIC ```python
# MAGIC # MAGIC %run ../_shared/surrogate_keys
# MAGIC
# MAGIC keyed = flat.withColumn("TRAN_SEQ_NO", tran_seq_no_expr())
# MAGIC ```
# MAGIC
# MAGIC ## Contract — what the caller must have already done
# MAGIC The DataFrame passed in must carry these three columns with these exact names and types:
# MAGIC
# MAGIC | Column                 | Type        | Source                                                   |
# MAGIC |---|---|---|
# MAGIC | `RTLOG_ORIG_SYS`       | `string`    | top-level bronze field (e.g. `'POS'`, `'MKT'`, `'OLIST'`) |
# MAGIC | `TRAN_SEQ_NO_NATURAL`  | `string`    | POS-assigned natural composite (e.g. `"STR33487-TILL02-2026-04-10-001234"`) |
# MAGIC | `TRAN_DATETIME`        | `timestamp` | bronze string **already cast** to `TimestampType()` — that cast is part of the hash |
# MAGIC
# MAGIC The flattening of those three columns is source-specific (POS pulls from `tran_head.*`;
# MAGIC marketplace and Olist will pull from different bronze shapes) and stays in each
# MAGIC source notebook. Only the hash formula is centralized.

# COMMAND ----------

from pyspark.sql.functions import xxhash64, col


def tran_seq_no_expr():
    """The canonical TRAN_SEQ_NO surrogate.

    Returns a Column expression suitable for `.withColumn("TRAN_SEQ_NO", ...)`.

    The argument ORDER, TYPES, and the TimestampType cast on TRAN_DATETIME
    are all load-bearing. Changing any of them changes every hash, which breaks
    every FK join across all six silver tables. If you ever need a different
    formula, ship it as `tran_seq_no_expr_v2()` and rebuild the silver layer —
    don't edit this in place.
    """
    return xxhash64(
        col("RTLOG_ORIG_SYS"),
        col("TRAN_SEQ_NO_NATURAL"),
        col("TRAN_DATETIME"),
    )

