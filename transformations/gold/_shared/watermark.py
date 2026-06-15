# Databricks notebook source
# MAGIC %md
# MAGIC # Gold `_shared` — `watermark.py`
# MAGIC
# MAGIC Per-fact watermark management against `gold_core._fact_load_log`. Loaded via
# MAGIC `%run ../_shared/watermark` (not import), same convention as silver helpers.
# MAGIC
# MAGIC ## Contract
# MAGIC - `get_watermark(fact_table) -> int` — highest successfully-processed silver
# MAGIC   `_commit_version` for this fact. Returns `-1` if no successful run yet.
# MAGIC - `start_fact_run(fact_table, source_table, version_from, version_to) -> run_id`
# MAGIC   — writes a RUNNING row, returns the generated run_id.
# MAGIC - `complete_fact_run(run_id, rows_inserted, rows_updated, rows_deleted)` — marks
# MAGIC   SUCCEEDED + sets end ts. The watermark only advances via SUCCEEDED rows.
# MAGIC - `fail_fact_run(run_id, error_message)` — marks FAILED; watermark unaffected.
# MAGIC
# MAGIC ## Why the log is the watermark
# MAGIC No separate watermark table. The watermark for fact X is, by definition,
# MAGIC `MAX(version_to)` over SUCCEEDED rows for X. A failed run leaves a FAILED row that
# MAGIC documents the attempt but doesn't move the cursor — so re-running re-reads the same
# MAGIC window. Idempotent by construction.

# COMMAND ----------

from datetime import datetime
import uuid as _uuid

from pyspark.sql import functions as _F

_LOG_TABLE = "retaildp.gold_core._fact_load_log"


def get_watermark(fact_table: str) -> int:
    """Highest successfully-processed silver _commit_version for this fact.
    Returns -1 if the fact has never had a SUCCEEDED run (caller treats -1 as
    'read from version 0 / full bootstrap')."""
    row = (
        spark.table(_LOG_TABLE)
        .where((_F.col("fact_table") == fact_table) & (_F.col("run_status") == "SUCCEEDED"))
        .agg(_F.max("version_to").alias("wm"))
        .collect()[0]
    )
    return int(row["wm"]) if row["wm"] is not None else -1


def _new_run_id(prefix: str = "FL") -> str:
    return f"{prefix}_{datetime.utcnow():%Y%m%d_%H%M%S}_{_uuid.uuid4().hex[:8]}"


def start_fact_run(fact_table: str, source_table: str,
                   version_from: int, version_to: int) -> str:
    """Write a RUNNING row and return the run_id."""
    run_id = _new_run_id("FL")
    spark.sql(f"""
        INSERT INTO {_LOG_TABLE}
            (run_id, fact_table, source_table, version_from, version_to,
             rows_inserted, rows_updated, rows_deleted,
             run_start_ts, run_end_ts, run_status, error_message)
        VALUES
            ('{run_id}', '{fact_table}', '{source_table}', {version_from}, {version_to},
             NULL, NULL, NULL,
             current_timestamp(), NULL, 'RUNNING', NULL)
    """)
    print(f"[watermark] started run {run_id} for {fact_table} "
          f"(versions {version_from}..{version_to})")
    return run_id


def complete_fact_run(run_id: str,
                      rows_inserted: int = 0,
                      rows_updated: int = 0,
                      rows_deleted: int = 0) -> None:
    """Mark a run SUCCEEDED. This is what advances the watermark."""
    spark.sql(f"""
        MERGE INTO {_LOG_TABLE} AS t
        USING (SELECT '{run_id}' AS run_id) AS s
        ON t.run_id = s.run_id
        WHEN MATCHED THEN UPDATE SET
            rows_inserted = {int(rows_inserted)},
            rows_updated  = {int(rows_updated)},
            rows_deleted  = {int(rows_deleted)},
            run_end_ts    = current_timestamp(),
            run_status    = 'SUCCEEDED'
    """)
    print(f"[watermark] completed run {run_id} "
          f"(ins={rows_inserted} upd={rows_updated} del={rows_deleted})")


def fail_fact_run(run_id: str, error_message: str) -> None:
    """Mark a run FAILED. Watermark is unaffected — re-run re-reads the same window."""
    safe_msg = (error_message or "")[:1000].replace("'", "''")
    spark.sql(f"""
        MERGE INTO {_LOG_TABLE} AS t
        USING (SELECT '{run_id}' AS run_id) AS s
        ON t.run_id = s.run_id
        WHEN MATCHED THEN UPDATE SET
            run_end_ts    = current_timestamp(),
            run_status    = 'FAILED',
            error_message = '{safe_msg}'
    """)
    print(f"[watermark] FAILED run {run_id}: {safe_msg[:120]}")


print("[watermark] helpers loaded: get_watermark, start_fact_run, complete_fact_run, fail_fact_run")

