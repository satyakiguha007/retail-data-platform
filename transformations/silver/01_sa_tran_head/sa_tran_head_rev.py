# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_tran_head_rev` (Delta CDF audit trail)
# MAGIC
# MAGIC `_REV` table in Oracle ReSA captures the prior revision of every row whose state
# MAGIC changed in the parent `SA_TRAN_HEAD`. The lakehouse-native equivalent of ReSA's
# MAGIC trigger-based _REV mechanism is **Delta Change Data Feed (CDF)**: opt the parent
# MAGIC table into CDF, then stream the change feed into a sibling Delta table that
# MAGIC carries the original schema + CDF metadata.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source** | `retaildp.silver.sa_tran_head` (via Delta CDF) |
# MAGIC | **Target** | `retaildp.silver.sa_tran_head_rev` |
# MAGIC | **Pattern** | `readStream` over change feed + `availableNow` + `foreachBatch` → append |
# MAGIC | **Idempotent** | Yes — checkpoint resumes from last processed commit version |
# MAGIC | **Scope** | v1 demo: scoped to `sa_tran_head` only |
# MAGIC | **Captures** | `update_preimage` + `delete` rows |
# MAGIC | **Skips**     | `update_postimage` (already in parent) + `insert` (no prior revision exists) |
# MAGIC
# MAGIC ## ReSA correspondence
# MAGIC
# MAGIC In real ReSA, `SA_TRAN_HEAD_REV` mirrors `SA_TRAN_HEAD` column-for-column, plus
# MAGIC a revision sequence. The "before" image of every UPDATEd row lands in `_REV`
# MAGIC at the moment of change; the live `SA_TRAN_HEAD` row holds the new value.
# MAGIC Audit queries reconstruct history by walking `_REV` backwards in time.
# MAGIC
# MAGIC This notebook produces the same effect using Delta primitives:
# MAGIC - `delta.enableChangeDataFeed = true` on the parent
# MAGIC - CDF emits a row per change with `_change_type` ∈ {`insert`, `update_preimage`, `update_postimage`, `delete`}
# MAGIC - We filter to `update_preimage` + `delete` and append to `_REV`
# MAGIC - `_commit_version` serves as the monotonic revision pointer
# MAGIC
# MAGIC ## Schema
# MAGIC
# MAGIC `_REV` schema = parent `sa_tran_head` schema (all columns, including the parent's
# MAGIC `_silver_ts`) **plus** four CDF/lineage columns:
# MAGIC
# MAGIC | Column | Source | Meaning |
# MAGIC |---|---|---|
# MAGIC | `_change_type` | CDF | `'update_preimage'` or `'delete'` |
# MAGIC | `_commit_version` | CDF | Delta version of the commit that produced this change |
# MAGIC | `_commit_timestamp` | CDF | Wall-clock of the change commit |
# MAGIC | `_rev_capture_ts` | this notebook | When _REV captured the revision (≥ `_commit_timestamp` by stream lag) |
# MAGIC
# MAGIC The parent's `_silver_ts` is preserved as-is on _REV — it documents *when the
# MAGIC pre-image was originally conformed*, which is forensically distinct from
# MAGIC *when the revision was captured*.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import col, current_timestamp, lit
from pyspark.sql.types import (
    StructType, StructField, LongType, StringType, TimestampType,
)
from delta.tables import DeltaTable

dbutils.widgets.text("parent_table", "retaildp.silver.sa_tran_head", "Parent Silver Table")
PARENT_TABLE = dbutils.widgets.get("parent_table")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

TARGET_TABLE    = "retaildp.silver.sa_tran_head_rev"
CHECKPOINT_PATH = (
    "abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/silver/sa_tran_head_rev/"
)

# CDF change_types we capture into _REV. Ignored types:
#   - 'insert'           — no prior revision exists; parent itself is the first revision
#   - 'update_postimage' — same as the current parent state; would be duplication
CAPTURED_CHANGE_TYPES = {"update_preimage", "delete"}

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — enable CDF on the parent (idempotent)
# MAGIC
# MAGIC `delta.enableChangeDataFeed = true` is the prerequisite. Setting it twice is a
# MAGIC no-op, so we ALTER unconditionally — simpler than introspecting `TBLPROPERTIES`
# MAGIC first. CDF only emits records for commits **after** the ALTER, so historical
# MAGIC changes (before CDF was enabled) are not captured. That's expected — the audit
# MAGIC trail starts now.

# COMMAND ----------

spark.sql(f"""
    ALTER TABLE {PARENT_TABLE}
    SET TBLPROPERTIES (delta.enableChangeDataFeed = true)
""")
print(f"CDF enabled on {PARENT_TABLE}")

# Quick confirmation
props = spark.sql(f"SHOW TBLPROPERTIES {PARENT_TABLE}").collect()
cdf_prop = [p for p in props if p.key == "delta.enableChangeDataFeed"]
print(f"Confirmed: {cdf_prop}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — derive _REV schema from parent + CDF metadata

# COMMAND ----------

parent_schema = spark.table(PARENT_TABLE).schema
PARENT_COLUMNS = [f.name for f in parent_schema.fields]

# _REV schema = parent fields + CDF metadata + capture timestamp
sa_tran_head_rev_schema = StructType(
    parent_schema.fields + [
        StructField("_change_type",      StringType(),    nullable=False),
        StructField("_commit_version",   LongType(),      nullable=False),
        StructField("_commit_timestamp", TimestampType(), nullable=False),
        StructField("_rev_capture_ts",   TimestampType(), nullable=False),
    ]
)

print(f"Parent has {len(PARENT_COLUMNS)} columns.")
print(f"_REV schema has {len(sa_tran_head_rev_schema.fields)} columns (parent + 4 CDF/lineage).")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — bootstrap _REV table if absent
# MAGIC
# MAGIC Partition by `BUSINESS_DATE` to mirror the parent. _REV rows belong to the
# MAGIC business date of the transaction they describe — keeps audit queries on
# MAGIC specific business dates efficient (partition pruning).

# COMMAND ----------

if not spark.catalog.tableExists(TARGET_TABLE):
    print(f"Creating empty {TARGET_TABLE} partitioned by BUSINESS_DATE.")
    (
        spark.createDataFrame([], sa_tran_head_rev_schema).write
        .format("delta")
        .partitionBy("BUSINESS_DATE")
        .option("delta.autoOptimize.optimizeWrite", "true")
        .option("delta.autoOptimize.autoCompact",   "true")
        .saveAsTable(TARGET_TABLE)
    )
else:
    print(f"{TARGET_TABLE} already exists — skipping bootstrap.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — foreachBatch handler
# MAGIC
# MAGIC Per CDF micro-batch:
# MAGIC 1. Filter to captured change types (`update_preimage` + `delete`).
# MAGIC 2. Stamp `_rev_capture_ts`.
# MAGIC 3. Project to target column order.
# MAGIC 4. Append.

# COMMAND ----------

def write_rev_batch(microBatchDF: DataFrame, batch_id: int) -> None:
    # 1. Filter to captured change types
    filtered = microBatchDF.where(col("_change_type").isin(*CAPTURED_CHANGE_TYPES))

    captured_n = filtered.count()
    if captured_n == 0:
        print(f"Batch {batch_id}: no changes to capture this batch.")
        return

    # 2. Add the capture timestamp
    stamped = filtered.withColumn("_rev_capture_ts", current_timestamp())

    # 3. Project to the target column order — parent columns + 4 CDF/lineage
    target_columns = [f.name for f in sa_tran_head_rev_schema.fields]
    projected = stamped.select(*target_columns)

    # 4. Append to _REV (no MERGE — _REV is append-only by design)
    projected.write.format("delta").mode("append").saveAsTable(TARGET_TABLE)

    # Diagnostic per batch
    by_type = (
        filtered.groupBy("_change_type").count()
        .collect()
    )
    summary = ", ".join(f"{r._change_type}={r['count']}" for r in by_type)
    print(f"Batch {batch_id}: captured {captured_n} rows ({summary})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — run the CDF stream
# MAGIC
# MAGIC `readChangeFeed=true` flips the DataFrame read from "current state" to "change
# MAGIC stream". We deliberately **do NOT** pass `startingVersion` — the parent table
# MAGIC has Delta versions that pre-date CDF enablement (POS Pass-1, MKT Pass-2, Olist
# MAGIC Pass-3 each added commits before this notebook ever ran). Asking for those
# MAGIC versions raises `DELTA_MISSING_CHANGE_DATA` because CDF wasn't recording them.
# MAGIC
# MAGIC Without `startingVersion`, the stream starts from the **current** version. The
# MAGIC checkpoint at `CHECKPOINT_PATH` tracks where we left off, so subsequent reruns
# MAGIC only pick up new commits — which is exactly the audit-trail-from-now-forward
# MAGIC semantics ReSA's `_REV` provides.

# COMMAND ----------

# DIAGNOSTIC — see when CDF was first enabled on the parent.
# Look for the most recent SET TBLPROPERTIES with delta.enableChangeDataFeed=true;
# the `version` column on that row is the floor of CDF data availability.
print("=== Parent table history (last 5 commits) ===")
spark.sql(f"DESCRIBE HISTORY {PARENT_TABLE} LIMIT 5").show(truncate=False)

# COMMAND ----------

print(f"Stream checkpoint: {CHECKPOINT_PATH}")

(
    spark.readStream
    .format("delta")
    .option("readChangeFeed", "true")
    # No startingVersion — stream from current version forward
    .table(PARENT_TABLE)
    .writeStream
    .foreachBatch(write_rev_batch)
    .trigger(availableNow=True)
    .option("checkpointLocation", CHECKPOINT_PATH)
    .start()
    .awaitTermination()
)

print("Stream complete.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Validation

# COMMAND ----------

rev_count = spark.table(TARGET_TABLE).count()
print(f"silver.sa_tran_head_rev total rows: {rev_count:,}")

if rev_count == 0:
    print("\nNo revisions captured yet — this is expected on first run if no UPDATEs or DELETEs")
    print("have hit sa_tran_head since CDF was enabled. See the optional demo block below.")
else:
    # 1. Distribution by change type
    print("\nChange type distribution:")
    spark.table(TARGET_TABLE).groupBy("_change_type").count().orderBy(col("count").desc()).show()

    # 2. Distribution by channel (which channel's rows are being audited)
    print("\nChannel distribution of captured revisions:")
    spark.table(TARGET_TABLE).groupBy("RTLOG_ORIG_SYS").count().orderBy(col("count").desc()).show()

    # 3. Commit version range — how many distinct commits produced revisions
    print("\nCommit version range:")
    spark.table(TARGET_TABLE).selectExpr(
        "MIN(_commit_version) AS min_version",
        "MAX(_commit_version) AS max_version",
        "COUNT(DISTINCT _commit_version) AS distinct_commits",
    ).show()

    # 4. Sample — most recent 5 revisions
    print("\nMost recent 5 revisions:")
    (
        spark.table(TARGET_TABLE)
        .select(
            "TRAN_SEQ_NO", "RTLOG_ORIG_SYS", "TRAN_TYPE", "VALUE",
            "_change_type", "_commit_version", "_commit_timestamp", "_rev_capture_ts",
        )
        .orderBy(col("_commit_version").desc(), col("_change_type"))
        .show(5, truncate=False)
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Optional demo — trigger a revision to verify capture
# MAGIC
# MAGIC Uncomment and run **once** to validate the CDF flow end-to-end. The block:
# MAGIC 1. Picks a random POS row from `sa_tran_head`.
# MAGIC 2. UPDATEs its `ERROR_IND` to `'A'` — a low-impact change that doesn't break FKs.
# MAGIC 3. Prompts you to re-run cells 5 (stream) + the validation block above.
# MAGIC 4. After re-run, `_REV` should have 1 new row with `_change_type='update_preimage'`.
# MAGIC
# MAGIC The UPDATE preserves the rest of the row, so re-running the entire ingestion
# MAGIC pipeline doesn't undo anything. To revert, manually UPDATE the same TRAN_SEQ_NO
# MAGIC back to the original ERROR_IND value (NULL for POS).

# COMMAND ----------

# # --- UNCOMMENT TO RUN ---
#sample_row = (
#    spark.table(PARENT_TABLE)
#    .where(col("RTLOG_ORIG_SYS") == "POS")
#    .select("TRAN_SEQ_NO", "ERROR_IND")
##     .limit(1)
 #    .collect()
# )
#if sample_row:
#    test_seq = sample_row[0].TRAN_SEQ_NO
#   orig_err = sample_row[0].ERROR_IND
#    print(f"Updating TRAN_SEQ_NO={test_seq} ERROR_IND: {orig_err!r} -> 'A'")
#    spark.sql(f"""
 #       UPDATE {PARENT_TABLE}
#        SET ERROR_IND = 'A'
 #       WHERE TRAN_SEQ_NO = {test_seq}
#    """)
#    print("UPDATE committed. Re-run the stream cell + validation block above.")
#    print(f"Expected: 1 new _REV row with _change_type='update_preimage', "
##          f"TRAN_SEQ_NO={test_seq}, ERROR_IND={orig_err!r}")
#else:
#    print("No POS rows available — load #some data first.")
