# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — `09_fact_store_day.py`
# MAGIC
# MAGIC Store-day aggregate fact. One row per `(store, business_date)`. **Derived**, not a
# MAGIC silver passthrough — re-aggregated from `gold_core.fact_sales_line`.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Target** | `retaildp.gold_core.fact_store_day` |
# MAGIC | **Grain** | One row per `(store_key, date_key, channel_key)` |
# MAGIC | **Source** | `gold_core.fact_sales_line` (re-aggregated) + `gold_core.fact_audit_error` (error counts) |
# MAGIC | **Merge key** | `(store_key, date_key, channel_key)` |
# MAGIC | **Clustering** | Liquid Clustering on `(business_date, store_key)` |
# MAGIC
# MAGIC ## Why this fact is different
# MAGIC The other three facts are CDF passthroughs (one fact row per source row). This one
# MAGIC AGGREGATES — many sales lines collapse to one store-day row. Its "incremental" trigger
# MAGIC is therefore different: it recomputes the set of `(store, business_date)` that changed
# MAGIC in `fact_sales_line` since the last store-day run, then DELETE+INSERTs just those
# MAGIC store-days. This is the store-day-as-unit-of-work pattern that the Liquid Clustering
# MAGIC key `(business_date, store_key)` is designed for.
# MAGIC
# MAGIC ## Incremental strategy
# MAGIC 1. Read fact_sales_line CDF since this fact's watermark → distinct affected
# MAGIC    `(business_date, store_key)` tuples.
# MAGIC 2. Re-aggregate THOSE store-days fully from the current fact_sales_line state.
# MAGIC 3. DELETE the affected store-days from the target, INSERT the recomputed rows.
# MAGIC 4. Advance the watermark to fact_sales_line's current version.
# MAGIC
# MAGIC On first run (watermark -1) every store-day is "affected" → full build.
# MAGIC
# MAGIC ## Measures
# MAGIC - `gross_sales_usd` — SUM of positive-qty line revenue (sales)
# MAGIC - `returns_usd` — SUM of negative-qty line revenue magnitude (returns)
# MAGIC - `net_sales_usd` — gross - returns
# MAGIC - `line_count`, `tran_count` — volume
# MAGIC - `error_count` — joined from fact_audit_error for the store-day
# MAGIC
# MAGIC ## ⚠ Dependency
# MAGIC Run AFTER fact_sales_line and fact_audit_error. It reads both.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 0. ⚠ One-time cleanup (only if rebuilding)

# COMMAND ----------

#spark.sql("DROP TABLE IF EXISTS retaildp.gold_core.fact_store_day")
#spark.sql("DELETE FROM retaildp.gold_core._fact_load_log WHERE fact_table = 'fact_store_day'")
#print("Dropped fact_store_day + cleared watermark. Next run bootstraps.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration + helpers

# COMMAND ----------

CATALOG       = "retaildp"
CORE          = "gold_core"
FACT          = "fact_store_day"
FACT_FQN      = f"{CATALOG}.{CORE}.{FACT}"

SALES_FACT    = f"{CATALOG}.{CORE}.fact_sales_line"   # aggregation source + CDF driver
AUDIT_FACT    = f"{CATALOG}.{CORE}.fact_audit_error"  # error_count join

print(f"Target:      {FACT_FQN}")
print(f"Sales src:   {SALES_FACT}")
print(f"Audit src:   {AUDIT_FACT}")

# COMMAND ----------

# MAGIC %run ../gold/_shared/watermark

# COMMAND ----------

# MAGIC %run ../gold/_shared/cdf_reader

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Create `fact_store_day`

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {FACT_FQN} (
    date_key         BIGINT     COMMENT 'FK dim_date',
    store_key        BIGINT     COMMENT 'FK dim_store',
    channel_key      BIGINT     COMMENT 'FK dim_channel (the store-day''s channel)',

    business_date    DATE       NOT NULL  COMMENT 'Clustering key',
    store            BIGINT     NOT NULL  COMMENT 'Raw store (degenerate)',
    rtlog_orig_sys   STRING               COMMENT 'Raw channel (degenerate)',

    gross_sales_usd  DECIMAL(20,4)  COMMENT 'SUM of positive-qty line revenue, USD',
    returns_usd      DECIMAL(20,4)  COMMENT 'SUM of negative-qty line revenue magnitude, USD',
    net_sales_usd    DECIMAL(20,4)  COMMENT 'gross_sales_usd - returns_usd',
    line_count       BIGINT         COMMENT 'Number of sales lines',
    tran_count       BIGINT         COMMENT 'Distinct transactions',
    error_count      BIGINT         COMMENT 'Audit findings for this store-day (from fact_audit_error)',

    _load_ts         TIMESTAMP  NOT NULL  COMMENT 'When loaded/updated',
    _source          STRING               COMMENT 'Derived from fact_sales_line + fact_audit_error'
)
USING DELTA
CLUSTER BY (business_date, store_key)
TBLPROPERTIES (
    'delta.enableChangeDataFeed'       = 'true',
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.autoOptimize.autoCompact'   = 'true'
)
COMMENT 'Store-day aggregate fact. Derived from fact_sales_line + fact_audit_error. Grain (store_key, date_key).'
""")

print(f"{FACT_FQN} ready.")

# COMMAND ----------

# Idempotent CDF enable — handles tables created before CDF was added to the CREATE.
# No-op if already enabled. Required because downstream gold consumers read this fact via CDF.
spark.sql(f"ALTER TABLE {FACT_FQN} SET TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true')")
print(f"CDF enabled on {FACT_FQN}.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Determine affected store-days via fact_sales_line CDF

# COMMAND ----------

from pyspark.sql import functions as F

last_wm = get_watermark(FACT)
print(f"Current watermark for {FACT}: {last_wm}")

# CDF on the SALES FACT tells us which store-days changed.
cdf_df, version_to = read_cdf_since(SALES_FACT, last_wm)
version_from = last_wm + 1

cdf_count = cdf_df.count()
print(f"Window: v{version_from}..v{version_to}  |  fact_sales_line changes: {cdf_count:,}")

affected = None
n_affected = 0
if cdf_count > 0:
    affected = cdf_df.select("business_date", "store_key").distinct()
    n_affected = affected.count()
    print(f"Affected store-days: {n_affected:,}")
    if last_wm < 0:
        print("(bootstrap — all store-days)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Re-aggregate the affected store-days from current fact_sales_line state
# MAGIC
# MAGIC We aggregate the FULL current state for the affected store-days (not just the CDF
# MAGIC delta) so each store-day row is always a complete, correct total.

# COMMAND ----------

run_id = None
recomputed = None
n_recomputed = 0

if cdf_count == 0:
    print("No fact_sales_line changes — nothing to recompute. Watermark unchanged.")
else:
    run_id = start_fact_run(FACT, SALES_FACT, version_from, version_to)
    try:
        affected.createOrReplaceTempView("affected_store_days")

        # Aggregate sales for affected store-days from the CURRENT fact state.
        # fact_sales_line has store_key (surrogate) but no raw `store`; pull raw store
        # from dim_store for the degenerate column.
        sales_agg = spark.sql(f"""
            SELECT
                s.date_key,
                s.store_key,
                s.channel_key,
                s.business_date,
                d.store,
                s.rtlog_orig_sys,
                CAST(SUM(CASE WHEN s.qty > 0 THEN s.gross_amt_usd ELSE 0 END) AS DECIMAL(20,4)) AS gross_sales_usd,
                CAST(SUM(CASE WHEN s.qty < 0 THEN ABS(s.gross_amt_usd) ELSE 0 END) AS DECIMAL(20,4)) AS returns_usd,
                COUNT(*)                              AS line_count,
                COUNT(DISTINCT s.tran_seq_no)         AS tran_count
            FROM {SALES_FACT} s
            JOIN affected_store_days a
              ON s.business_date = a.business_date AND s.store_key = a.store_key
            LEFT JOIN {CATALOG}.{CORE}.dim_store d
              ON s.store_key = d.store_key
            GROUP BY s.date_key, s.store_key, s.channel_key, s.business_date, d.store, s.rtlog_orig_sys
        """)
        sales_agg.createOrReplaceTempView("sales_agg")

        # Error counts for the same affected store-days (left join — many store-days have none).
        recomputed = spark.sql(f"""
            SELECT
                sa.date_key,
                sa.store_key,
                sa.channel_key,
                sa.business_date,
                sa.store,
                sa.rtlog_orig_sys,
                sa.gross_sales_usd,
                sa.returns_usd,
                CAST(sa.gross_sales_usd - sa.returns_usd AS DECIMAL(20,4)) AS net_sales_usd,
                sa.line_count,
                sa.tran_count,
                COALESCE(ec.error_count, 0)            AS error_count,
                current_timestamp()                    AS _load_ts,
                'fact_sales_line+fact_audit_error'     AS _source
            FROM sales_agg sa
            LEFT JOIN (
                SELECT store_key, date_key, channel_key, SUM(error_count) AS error_count
                FROM {AUDIT_FACT}
                GROUP BY store_key, date_key, channel_key
            ) ec
              ON  sa.store_key   = ec.store_key
              AND sa.date_key    = ec.date_key
              AND sa.channel_key = ec.channel_key
        """)
        recomputed.createOrReplaceTempView("fact_store_day_recomputed")
        n_recomputed = recomputed.count()
        print(f"Recomputed store-day rows: {n_recomputed:,}")

    except Exception as e:
        fail_fact_run(run_id, str(e))
        raise

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. DELETE affected store-days + INSERT recomputed
# MAGIC
# MAGIC DELETE-then-INSERT (not MERGE) because the grain is an aggregate: we replace whole
# MAGIC store-day rows rather than updating individual measures. The affected set bounds the
# MAGIC DELETE so it's surgical.

# COMMAND ----------

if cdf_count > 0:
    try:
        # DELETE affected store-day-channels (so re-insert is clean). The grain is
        # (store_key, date_key, channel_key) — a real store can transact on both POS and
        # MKT in one day, so each channel is its own store-day row.
        spark.sql(f"""
            DELETE FROM {FACT_FQN} t
            WHERE EXISTS (
                SELECT 1 FROM affected_store_days a
                WHERE t.business_date = a.business_date AND t.store_key = a.store_key
            )
        """)

        before = spark.table(FACT_FQN).count()
        spark.sql(f"INSERT INTO {FACT_FQN} SELECT * FROM fact_store_day_recomputed")
        after = spark.table(FACT_FQN).count()

        rows_inserted = n_recomputed
        complete_fact_run(run_id, rows_inserted=rows_inserted, rows_updated=0, rows_deleted=0)
        print(f"DELETE+INSERT done. store-day rows now: {after:,}")

    except Exception as e:
        fail_fact_run(run_id, str(e))
        raise

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Validation

# COMMAND ----------

print("=== Store-day count + channel split ===")
display(spark.sql(f"""
    SELECT rtlog_orig_sys, COUNT(*) AS store_days,
           ROUND(SUM(net_sales_usd),2) AS net_usd
    FROM {FACT_FQN} GROUP BY rtlog_orig_sys ORDER BY rtlog_orig_sys
"""))

# COMMAND ----------

print("=== PK uniqueness on (store_key, date_key, channel_key) ===")
display(spark.sql(f"""
    SELECT COUNT(*) AS duplicate_keys FROM (
        SELECT store_key, date_key, channel_key, COUNT(*) c FROM {FACT_FQN}
        GROUP BY store_key, date_key, channel_key HAVING COUNT(*) > 1
    )
"""))

# COMMAND ----------

print("=== Reconciliation: store-day net == fact_sales_line net (USD) ===")
display(spark.sql(f"""
    SELECT
        (SELECT ROUND(SUM(net_sales_usd),2) FROM {FACT_FQN})                          AS store_day_net,
        (SELECT ROUND(SUM(CASE WHEN qty>0 THEN gross_amt_usd ELSE -ABS(gross_amt_usd) END),2)
         FROM {SALES_FACT})                                                            AS sales_line_net
"""))

# COMMAND ----------

print("=== Top 10 store-days by net sales ===")
display(spark.sql(f"""
    SELECT business_date, store, rtlog_orig_sys,
           gross_sales_usd, returns_usd, net_sales_usd,
           line_count, tran_count, error_count
    FROM {FACT_FQN}
    ORDER BY net_sales_usd DESC LIMIT 10
"""))

# COMMAND ----------

print("=== Store-days with audit findings ===")
display(spark.sql(f"""
    SELECT COUNT(*) AS store_days_with_errors, SUM(error_count) AS total_errors
    FROM {FACT_FQN} WHERE error_count > 0
"""))

# COMMAND ----------

print("=== Error-count reconciliation: store_day total == audit fact total ===")
display(spark.sql(f"""
    SELECT
        (SELECT SUM(error_count) FROM {FACT_FQN})    AS store_day_total,
        (SELECT SUM(error_count) FROM {AUDIT_FACT})  AS audit_fact_total
"""))

# COMMAND ----------

print("=== Watermark log ===")
display(spark.sql(f"""
    SELECT run_id, version_from, version_to, rows_inserted, run_status
    FROM {CATALOG}.{CORE}._fact_load_log
    WHERE fact_table = '{FACT}' ORDER BY run_start_ts DESC LIMIT 5
"""))

# COMMAND ----------

print(f"✅ fact_store_day load complete.")
print(f"   Store-day rows: {spark.table(FACT_FQN).count():,}")
print(f"   New watermark:  v{version_to}")
