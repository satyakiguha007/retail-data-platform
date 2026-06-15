# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — `07_fact_sales_line.py`
# MAGIC
# MAGIC The anchor fact. One row per sales line. CDF-driven from `silver.sa_tran_item`.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Target** | `retaildp.gold_core.fact_sales_line` |
# MAGIC | **Grain** | One row per `(tran_seq_no, item_seq_no)` — a single item line |
# MAGIC | **Source** | `silver.sa_tran_item` (all 3 channels) via CDF |
# MAGIC | **Load** | Bootstrap (full) on first run; incremental CDF after |
# MAGIC | **Merge key** | `(tran_seq_no, item_seq_no)` (degenerate dimension) |
# MAGIC | **Clustering** | Liquid Clustering on `(business_date, store_key)` |
# MAGIC | **Watermark** | `gold_core._fact_load_log` via `_shared/watermark` |
# MAGIC
# MAGIC ## FKs
# MAGIC `date_key`, `store_key`, `item_key` (SCD2 current), `channel_key`. Unmatched → -1
# MAGIC (unknown member), never dropped.
# MAGIC
# MAGIC ## Measures
# MAGIC - `qty` — units (DECIMAL 12,4)
# MAGIC - `unit_retail`, `unit_retail_usd` — per-unit price, local + USD
# MAGIC - `gross_amt = qty * unit_retail`, `gross_amt_usd` — line revenue
# MAGIC - `total_igtax_amt`, `total_igtax_amt_usd` — tax where present (NULL for MKT/OMS = "not measured")
# MAGIC
# MAGIC **Discounts are NOT on this fact.** `sa_tran_disc` is at a different grain and isn't
# MAGIC universally item-aligned across channels. `gross_amt` here is pre-discount line value.
# MAGIC Discount allocation to the line is a documented future enhancement.
# MAGIC
# MAGIC ## Freight
# MAGIC The Olist synthetic freight line (`item='101010101'`, NMR) flows through as a normal
# MAGIC fact row (qty=1, unit_retail=freight_total). Freight is real revenue at line grain.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 0. ⚠ One-time cleanup (only if rebuilding from scratch)
# MAGIC
# MAGIC Dropping the fact also requires clearing its watermark rows so the next run bootstraps.
# MAGIC Leave commented for normal incremental runs.

# COMMAND ----------

# --- UNCOMMENT to fully rebuild ---
# spark.sql("DROP TABLE IF EXISTS retaildp.gold_core.fact_sales_line")
# spark.sql("DELETE FROM retaildp.gold_core._fact_load_log WHERE fact_table = 'fact_sales_line'")
# print("Dropped fact_sales_line + cleared its watermark. Next run bootstraps.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration + helpers

# COMMAND ----------

CATALOG      = "retaildp"
CORE         = "gold_core"
FACT         = "fact_sales_line"
FACT_FQN     = f"{CATALOG}.{CORE}.{FACT}"
SOURCE_TABLE = f"{CATALOG}.silver.sa_tran_item"

print(f"Target: {FACT_FQN}")
print(f"Source: {SOURCE_TABLE}")

# COMMAND ----------

# MAGIC %run ./_shared/watermark

# COMMAND ----------

# MAGIC %run ./_shared/cdf_reader

# COMMAND ----------

# MAGIC %run ./_shared/dim_lookup

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Create `fact_sales_line` (Liquid Clustering)
# MAGIC
# MAGIC `CLUSTER BY (business_date, store_key)` — the store-day access pattern, without the
# MAGIC partition-cardinality blow-up. No `DEFAULT` columns here, so no `allowColumnDefaults`
# MAGIC feature needed; `_load_ts` is set explicitly by the writer.

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {FACT_FQN} (
    -- Surrogate FKs to dimensions
    date_key             BIGINT     COMMENT 'FK dim_date (-1 = unknown)',
    store_key            BIGINT     COMMENT 'FK dim_store (-1 = unknown)',
    item_key             BIGINT     COMMENT 'FK dim_item current version (-1 = unknown)',
    channel_key          BIGINT     COMMENT 'FK dim_channel (-1 = unknown)',

    -- Degenerate dimensions (natural keys carried on the fact)
    tran_seq_no          BIGINT     NOT NULL  COMMENT 'Transaction surrogate from silver',
    item_seq_no          INT        NOT NULL  COMMENT 'Line number within transaction',
    business_date        DATE       NOT NULL  COMMENT 'Clustering key; also FK-resolved to date_key',
    rtlog_orig_sys       STRING               COMMENT 'Raw channel discriminator (degenerate)',
    item                 STRING               COMMENT 'Raw SKU (degenerate; item_key is the FK)',

    -- Measures
    qty                  DECIMAL(12,4)  COMMENT 'Units on this line',
    unit_retail          DECIMAL(20,4)  COMMENT 'Per-unit price, local currency',
    unit_retail_usd      DECIMAL(20,4)  COMMENT 'Per-unit price, USD',
    gross_amt            DECIMAL(20,4)  COMMENT 'qty * unit_retail, local (pre-discount)',
    gross_amt_usd        DECIMAL(20,4)  COMMENT 'qty * unit_retail_usd, USD',
    total_igtax_amt      DECIMAL(20,4)  COMMENT 'Line IGTAX, local (NULL = not measured)',
    total_igtax_amt_usd  DECIMAL(20,4)  COMMENT 'Line IGTAX, USD (NULL = not measured)',

    -- Lineage
    _load_ts             TIMESTAMP  NOT NULL  COMMENT 'When this fact row was loaded/updated',
    _source              STRING               COMMENT 'Source table'
)
USING DELTA
CLUSTER BY (business_date, store_key)
TBLPROPERTIES (
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.autoOptimize.autoCompact'   = 'true'
)
COMMENT 'Anchor fact — one row per sales line. CDF-driven from silver.sa_tran_item. Grain (tran_seq_no, item_seq_no).'
""")

print(f"{FACT_FQN} ready.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Read source via CDF + resolve watermark window

# COMMAND ----------

last_wm = get_watermark(FACT)
print(f"Current watermark for {FACT}: {last_wm}")

cdf_df, version_to = read_cdf_since(SOURCE_TABLE, last_wm)
version_from = last_wm + 1

src_count = cdf_df.count()
print(f"Window: v{version_from}..v{version_to}  |  source rows: {src_count:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Build fact rows — measures + dim keys
# MAGIC
# MAGIC Short-circuit if there's nothing new (incremental no-op). Otherwise: compute measures,
# MAGIC attach the four surrogate keys via `_shared/dim_lookup`, project to the fact schema.

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.types import DecimalType

run_id = None
clean_n = 0

if src_count == 0:
    print("No new source rows — nothing to load. Watermark unchanged.")
else:
    run_id = start_fact_run(FACT, SOURCE_TABLE, version_from, version_to)

    try:
        # 4a. measures
        measured = (
            cdf_df
            .withColumn("gross_amt",
                        (F.col("QTY") * F.col("UNIT_RETAIL")).cast(DecimalType(20, 4)))
            .withColumn("gross_amt_usd",
                        (F.col("QTY") * F.col("UNIT_RETAIL_USD")).cast(DecimalType(20, 4)))
        )

        # 4b. dim keys (helpers default to silver column names)
        keyed = (
            measured
            .transform(add_date_key)       # BUSINESS_DATE -> date_key
            .transform(add_store_key)      # STORE -> store_key
            .transform(add_channel_key)    # RTLOG_ORIG_SYS -> channel_key
            .transform(add_item_key)       # ITEM -> item_key (is_current)
        )

        # 4c. project to fact schema (lowercase target columns)
        staged = keyed.select(
            F.col("date_key"),
            F.col("store_key"),
            F.col("item_key"),
            F.col("channel_key"),
            F.col("TRAN_SEQ_NO").alias("tran_seq_no"),
            F.col("ITEM_SEQ_NO").alias("item_seq_no"),
            F.col("BUSINESS_DATE").alias("business_date"),
            F.col("RTLOG_ORIG_SYS").alias("rtlog_orig_sys"),
            F.col("ITEM").alias("item"),
            F.col("QTY").alias("qty"),
            F.col("UNIT_RETAIL").alias("unit_retail"),
            F.col("UNIT_RETAIL_USD").alias("unit_retail_usd"),
            F.col("gross_amt"),
            F.col("gross_amt_usd"),
            F.col("TOTAL_IGTAX_AMT").alias("total_igtax_amt"),
            F.col("TOTAL_IGTAX_AMT_USD").alias("total_igtax_amt_usd"),
            F.current_timestamp().alias("_load_ts"),
            F.lit(SOURCE_TABLE).alias("_source"),
        )

        # Guard: drop any rows with NULL merge keys (shouldn't happen — PK in silver)
        staged = staged.where(
            F.col("tran_seq_no").isNotNull() & F.col("item_seq_no").isNotNull()
        )

        staged.createOrReplaceTempView("fact_sales_line_staged")
        clean_n = staged.count()
        print(f"Staged fact rows: {clean_n:,}")

    except Exception as e:
        fail_fact_run(run_id, str(e))
        raise

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. MERGE into the fact
# MAGIC
# MAGIC `whenMatched → updateAll` handles silver `_REV`-style corrections (CDF
# MAGIC update_postimage). `whenNotMatched → insertAll` for new lines.

# COMMAND ----------

if src_count > 0:
    try:
        before = spark.table(FACT_FQN).count()

        spark.sql(f"""
            MERGE INTO {FACT_FQN} AS tgt
            USING fact_sales_line_staged AS src
            ON tgt.tran_seq_no = src.tran_seq_no
           AND tgt.item_seq_no = src.item_seq_no
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *
        """)

        after = spark.table(FACT_FQN).count()
        rows_inserted = after - before
        rows_updated  = clean_n - rows_inserted   # remainder were matches
        if rows_updated < 0:
            rows_updated = 0

        complete_fact_run(run_id, rows_inserted=rows_inserted,
                          rows_updated=rows_updated, rows_deleted=0)
        print(f"MERGE done. inserted={rows_inserted:,} updated≈{rows_updated:,}")

    except Exception as e:
        fail_fact_run(run_id, str(e))
        raise

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Validation

# COMMAND ----------

print("=== Fact row count + channel split ===")
display(spark.sql(f"""
    SELECT rtlog_orig_sys, COUNT(*) AS rows
    FROM {FACT_FQN}
    GROUP BY rtlog_orig_sys
    ORDER BY rtlog_orig_sys
"""))

# COMMAND ----------

print("=== Orphan FK check (any -1 keys?) ===")
display(spark.sql(f"""
    SELECT
        SUM(CASE WHEN date_key    = -1 THEN 1 ELSE 0 END) AS orphan_date,
        SUM(CASE WHEN store_key   = -1 THEN 1 ELSE 0 END) AS orphan_store,
        SUM(CASE WHEN item_key    = -1 THEN 1 ELSE 0 END) AS orphan_item,
        SUM(CASE WHEN channel_key = -1 THEN 1 ELSE 0 END) AS orphan_channel
    FROM {FACT_FQN}
"""))

# COMMAND ----------

print("=== PK uniqueness on (tran_seq_no, item_seq_no) — should be 0 dups ===")
display(spark.sql(f"""
    SELECT COUNT(*) AS duplicate_keys FROM (
        SELECT tran_seq_no, item_seq_no, COUNT(*) c
        FROM {FACT_FQN}
        GROUP BY tran_seq_no, item_seq_no
        HAVING COUNT(*) > 1
    )
"""))

# COMMAND ----------

print("=== Row-count reconciliation: fact == silver sa_tran_item ===")
display(spark.sql(f"""
    SELECT
        (SELECT COUNT(*) FROM {SOURCE_TABLE})  AS silver_rows,
        (SELECT COUNT(*) FROM {FACT_FQN})      AS fact_rows
"""))

# COMMAND ----------

print("=== Revenue sanity by channel (USD) ===")
display(spark.sql(f"""
    SELECT
        rtlog_orig_sys,
        COUNT(*)                       AS lines,
        ROUND(SUM(gross_amt_usd), 2)   AS gross_usd,
        ROUND(AVG(unit_retail_usd), 2) AS avg_unit_usd
    FROM {FACT_FQN}
    GROUP BY rtlog_orig_sys
    ORDER BY gross_usd DESC
"""))

# COMMAND ----------

print("=== Watermark log (latest 5 runs for this fact) ===")
display(spark.sql(f"""
    SELECT run_id, version_from, version_to, rows_inserted, rows_updated,
           run_status, run_start_ts, run_end_ts
    FROM {CATALOG}.{CORE}._fact_load_log
    WHERE fact_table = '{FACT}'
    ORDER BY run_start_ts DESC
    LIMIT 5
"""))

# COMMAND ----------

print(f"✅ fact_sales_line load complete.")
print(f"   Fact rows:    {spark.table(FACT_FQN).count():,}")
print(f"   New watermark: v{version_to}")
