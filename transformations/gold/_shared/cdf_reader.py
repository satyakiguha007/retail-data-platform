# Databricks notebook source
# MAGIC %md
# MAGIC # Gold `_shared` — `cdf_reader.py`
# MAGIC
# MAGIC Reads silver Change Data Feed between a watermark and the table's current version.
# MAGIC Loaded via `%run ../_shared/cdf_reader`.
# MAGIC
# MAGIC ## Contract
# MAGIC - `current_version(table) -> int` — latest Delta `_commit_version` of a table.
# MAGIC - `read_cdf_since(table, last_version) -> (DataFrame, version_to)` — returns the CDF
# MAGIC   rows representing current truth (insert + update_postimage) for the window
# MAGIC   `(last_version, current]`, plus the `version_to` the caller should record as the new
# MAGIC   watermark.
# MAGIC
# MAGIC ## First-run / bootstrap (`last_version == -1`)
# MAGIC CDF cannot be read from before the version where CDF was enabled. On the first load of a
# MAGIC fact, we don't use CDF at all — we read the full table as-of its current version and tag
# MAGIC every row `_change_type = 'insert'`. Subsequent runs read true CDF from the recorded
# MAGIC watermark forward. This sidesteps `DELTA_CHANGE_DATA_FEED_*` errors on the historical
# MAGIC pre-CDF range.
# MAGIC
# MAGIC ## Change types kept
# MAGIC `insert` and `update_postimage` only. `update_preimage` and `delete` are dropped —
# MAGIC the fact MERGE represents current state, and silver corrections arrive as
# MAGIC update_postimage. (Hard deletes in silver are not part of this project's model; if they
# MAGIC ever are, handle `delete` with a fact-side soft-delete here.)

# COMMAND ----------

from pyspark.sql import DataFrame as _DataFrame
from pyspark.sql import functions as _F

_KEPT_CHANGE_TYPES = ["insert", "update_postimage"]


def current_version(table: str) -> int:
    """Latest committed Delta version of a table."""
    v = (
        spark.sql(f"DESCRIBE HISTORY {table} LIMIT 1")
        .select("version")
        .collect()[0]["version"]
    )
    return int(v)


def read_cdf_since(table: str, last_version: int):
    """Return (df, version_to).

    df carries current-truth rows (insert + update_postimage) plus a `_change_type` column.
    version_to is the version the caller should persist as the new watermark on success.
    """
    version_to = current_version(table)

    # First run / bootstrap — no CDF, read full snapshot tagged as inserts.
    if last_version < 0:
        df = (
            spark.read.table(table)
            .withColumn("_change_type", _F.lit("insert"))
            .withColumn("_commit_version", _F.lit(version_to))
        )
        print(f"[cdf] {table}: BOOTSTRAP full read @ v{version_to} "
              f"({df.count():,} rows tagged insert)")
        return df, version_to

    # No new commits since last watermark — nothing to do.
    if version_to <= last_version:
        empty = spark.read.table(table).limit(0).withColumn("_change_type", _F.lit("insert"))
        print(f"[cdf] {table}: no new versions (watermark v{last_version} == current v{version_to})")
        return empty, version_to

    # Incremental — true CDF from (last_version, current].
    df = (
        spark.read.format("delta")
        .option("readChangeFeed", "true")
        .option("startingVersion", last_version + 1)
        .option("endingVersion", version_to)
        .table(table)
        .where(_F.col("_change_type").isin(_KEPT_CHANGE_TYPES))
    )
    print(f"[cdf] {table}: CDF v{last_version + 1}..v{version_to} "
          f"({df.count():,} insert/update_postimage rows)")
    return df, version_to


print("[cdf] helpers loaded: current_version, read_cdf_since")

