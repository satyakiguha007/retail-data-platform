# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_tran_item` (POS)
# MAGIC
# MAGIC Line-item detail. One row per (transaction × line item), conformed to the
# MAGIC ReSA-canonical `SA_TRAN_ITEM` schema. Child of `sa_tran_head` — the first child table.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source** | `retaildp.bronze.pos_rtlog` — `tran_item` is an `ARRAY<STRUCT>` |
# MAGIC | **Target** | `retaildp.silver.sa_tran_item` |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_tran_item_rejects` |
# MAGIC | **FK lookup** | `silver.sa_tran_head` (parent — validates FK AND carries `CURRENCY_CODE` / `FX_RATE`) |
# MAGIC | **Pattern** | `readStream` + `availableNow` + `foreachBatch` → MERGE |
# MAGIC | **Idempotent** | Yes — deterministic `TRAN_SEQ_NO` + MERGE on `(TRAN_SEQ_NO, ITEM_SEQ_NO)` |
# MAGIC | **Partitioned by** | `BUSINESS_DATE` (same as parent) |
# MAGIC
# MAGIC ## Shared helpers used (see `_shared/`)
# MAGIC - `surrogate_keys.tran_seq_no_expr()` — the canonical `TRAN_SEQ_NO` hash (cell 6 step 3)
# MAGIC - `fx_helpers.enrich_with_parent_fx()` — FK validate + inherit `CURRENCY_CODE` / `FX_RATE` (cell 6 step 4)
# MAGIC - `quarantine.merge_and_quarantine()` — idempotent MERGE + quarantine append (cell 6 step 7)
# MAGIC
# MAGIC ## Patterns introduced here (vs `01_sa_tran_head`)
# MAGIC 1. **`explode` the nested array** — one bronze row fans out to N item rows via `explode(tran_item)`. The header was 1:1 with the bronze row; items are 1:N.
# MAGIC 2. **FK join to the Silver parent, not a re-derivation** — `TRAN_SEQ_NO` is re-derived with the same shared formula (`tran_seq_no_expr()`), then we join `sa_tran_head` on `TRAN_SEQ_NO` alone (it's a globally-unique hash). An item whose header was rejected upstream is an orphan and is quarantined.
# MAGIC 3. **Inherit `CURRENCY_CODE` + `FX_RATE` from the parent** — the header already did the country→currency→FX lookup. We pull the rate down through the FK join so item USD figures stay consistent with the header's.
# MAGIC
# MAGIC ## Surrogate / composite key
# MAGIC `TRAN_SEQ_NO` derived via `tran_seq_no_expr()` (see `_shared/surrogate_keys.py`).
# MAGIC PK appends the POS-assigned line number: `(TRAN_SEQ_NO, ITEM_SEQ_NO)`.
# MAGIC
# MAGIC ## DQ rules (failures routed to quarantine)
# MAGIC 1. `orphan_no_parent_header` — `TRAN_SEQ_NO` has no match in `silver.sa_tran_head`
# MAGIC 2. `ITEM_SEQ_NO` is NOT NULL (PK)
# MAGIC 3. `ITEM_TYPE` is NOT NULL (mandatory in ReSA)
# MAGIC
# MAGIC ## NOT a DQ failure (kept as data, not quarantined)
# MAGIC - NULL `UNIT_RETAIL` — non-merch lines / vouchers legitimately have none
# MAGIC - NULL `ORIG_UNIT_RETAIL` (no price override), NULL flex / return fields
# MAGIC - `ERROR_IND = 'Y'` — fault-injected rows pass through; Module 4 audit catches them
# MAGIC - Items on `RETURN` / `PVOID` headers — the header carries the tran type; the line is still valid
# MAGIC - `FX_RATE` null → USD columns null → row still lands (only if `fx_rates` has a gap)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, explode, current_timestamp, lit, when,
    array, array_compact,
)
from pyspark.sql.types import (
    StructType, StructField, LongType, IntegerType, StringType,
    DateType, TimestampType, DecimalType, ArrayType,
)

dbutils.widgets.text("source_table", "retaildp.bronze.pos_rtlog", "Source Bronze Table")
SOURCE_TABLE = dbutils.widgets.get("source_table")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Shared helpers
# MAGIC
# MAGIC `%run` resolves the helper notebooks relative to this notebook's location.
# MAGIC Path from `transformations/silver/02_sa_tran_item/pos.py` → `_shared/` is `../_shared/`.

# COMMAND ----------

# MAGIC %run ../_shared/surrogate_keys

# COMMAND ----------

# MAGIC %run ../_shared/fx_helpers

# COMMAND ----------

# MAGIC %run ../_shared/quarantine

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

TARGET_TABLE     = "retaildp.silver.sa_tran_item"
QUARANTINE_TABLE = "retaildp.quarantine.silver_sa_tran_item_rejects"
PARENT_TABLE     = "retaildp.silver.sa_tran_head"   # FK + CURRENCY_CODE / FX_RATE carrier
CHECKPOINT_PATH  = (
    "abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/silver/sa_tran_item/"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema
# MAGIC
# MAGIC ReSA-canonical `SA_TRAN_ITEM` column names (trimmed to the relevant set, same pragmatism as `01`)
# MAGIC + lakehouse FX companions (`*_USD`, `FX_RATE`, `CURRENCY_CODE` inherited from parent).
# MAGIC No `DAY` column — `TRAN_SEQ_NO` is the unique surrogate, matching the parent. Lineage `_`-prefixed.

# COMMAND ----------

sa_tran_item_schema = StructType([
    # Identity & FK
    StructField("TRAN_SEQ_NO",          LongType(),        nullable=False),  # FK -> sa_tran_head (surrogate)
    StructField("ITEM_SEQ_NO",          IntegerType(),     nullable=False),  # POS line number
    StructField("RTLOG_ORIG_SYS",       StringType(),      nullable=False),  # POS / MKT / OLIST

    # Parent-context natural columns
    StructField("STORE",                LongType(),        nullable=False),
    StructField("BUSINESS_DATE",        DateType(),        nullable=False),  # partition

    # Item identity
    StructField("ITEM",                 StringType(),      nullable=True),   # SKU
    StructField("ITEM_STATUS",          StringType(),      nullable=True),
    StructField("ITEM_TYPE",            StringType(),      nullable=False),
    StructField("ITEM_SWIPED_IND",      StringType(),      nullable=True),
    StructField("DROP_SHIP_IND",        StringType(),      nullable=True),

    # Merch hierarchy
    StructField("DEPT",                 IntegerType(),     nullable=True),
    StructField("CLASS",                IntegerType(),     nullable=True),
    StructField("SUBCLASS",             IntegerType(),     nullable=True),

    # Quantities / price
    StructField("QTY",                  DecimalType(12, 4), nullable=True),
    StructField("UNIT_RETAIL",          DecimalType(20, 4), nullable=True),
    StructField("SELLING_UOM",          StringType(),      nullable=True),
    StructField("UOM_QUANTITY",         DecimalType(12, 4), nullable=True),
    StructField("ORIG_UNIT_RETAIL",     DecimalType(20, 4), nullable=True),
    StructField("OVERRIDE_REASON",      StringType(),      nullable=True),

    # Tax
    StructField("TAX_IND",              StringType(),      nullable=True),
    StructField("UNIT_RETAIL_VAT_INCL", StringType(),      nullable=True),
    StructField("TOTAL_IGTAX_AMT",      DecimalType(20, 4), nullable=True),

    # Returns / e-commerce linkage
    StructField("RETURN_REASON_CODE",   StringType(),      nullable=True),
    StructField("CUST_ORDER_NO",        StringType(),      nullable=True),

    # Flags
    StructField("ERROR_IND",            StringType(),      nullable=True),

    # FX (inherited rate + USD companions)
    StructField("CURRENCY_CODE",        StringType(),      nullable=False),  # from parent
    StructField("FX_RATE",              DecimalType(20, 6), nullable=True),
    StructField("UNIT_RETAIL_USD",      DecimalType(20, 4), nullable=True),
    StructField("ORIG_UNIT_RETAIL_USD", DecimalType(20, 4), nullable=True),
    StructField("TOTAL_IGTAX_AMT_USD",  DecimalType(20, 4), nullable=True),

    # Lineage
    StructField("_silver_ts",           TimestampType(),   nullable=False),
    StructField("_source",              StringType(),      nullable=False),
])

# Quarantine = target columns + rejection_reason + quarantine_ts (rejects keep the wide debug shape)
quarantine_schema = StructType(
    sa_tran_item_schema.fields + [
        StructField("rejection_reason", ArrayType(StringType()), nullable=False),
        StructField("_quarantine_ts",   TimestampType(),         nullable=False),
    ]
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bootstrap target table if absent

# COMMAND ----------

if not spark.catalog.tableExists(TARGET_TABLE):
    print(f"Creating empty {TARGET_TABLE} partitioned by BUSINESS_DATE.")
    (
        spark.createDataFrame([], sa_tran_item_schema).write
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
# MAGIC ## `merge_microbatch` — the heart
# MAGIC
# MAGIC Per micro-batch:
# MAGIC 1. **Explode** — `explode(tran_item)` fans one bronze row into N item rows.
# MAGIC 2. **Flatten** — pull `tran_head` (key) + `item.*` (attributes) into a flat shape with explicit aliases. `TRAN_DATETIME` cast to `TimestampType()` here so `tran_seq_no_expr()` produces the right hash.
# MAGIC 3. **Surrogate key** — `tran_seq_no_expr()` from `_shared/surrogate_keys`. Same hash across all silver tables.
# MAGIC 4. **FK enrich** — `enrich_with_parent_fx()` from `_shared/fx_helpers`. Broadcast-joins `sa_tran_head` on `TRAN_SEQ_NO`, inherits `CURRENCY_CODE` + `FX_RATE`, flags orphans via `_has_parent`.
# MAGIC 5. **Derive** — `*_USD = amount * FX_RATE`, lineage.
# MAGIC 6. **DQ split** — clean vs reject via `rejection_reason` array.
# MAGIC 7. **Write** — `merge_and_quarantine()` from `_shared/quarantine`. Idempotent MERGE on `(TRAN_SEQ_NO, ITEM_SEQ_NO)` + append to quarantine.

# COMMAND ----------

def merge_microbatch(microBatchDF: DataFrame, batch_id: int) -> None:
    # 1 + 2. Explode items, then flatten with explicit aliases
    flat = (
        microBatchDF
        .withColumn("item", explode(col("tran_item")))
        .select(
            # --- parent key components (must match the contract of tran_seq_no_expr) ---
            col("rtlog_orig_sys").alias("RTLOG_ORIG_SYS"),
            col("store").cast(LongType()).alias("STORE"),
            col("date").alias("BUSINESS_DATE"),
            col("tran_head.tran_seq_no").alias("TRAN_SEQ_NO_NATURAL"),
            col("tran_head.tran_datetime").cast(TimestampType()).alias("TRAN_DATETIME"),
            # --- item line ---
            col("item.item_seq_no").cast(IntegerType()).alias("ITEM_SEQ_NO"),
            col("item.item").alias("ITEM"),
            col("item.item_status").alias("ITEM_STATUS"),
            col("item.item_type").alias("ITEM_TYPE"),
            col("item.item_swiped_ind").alias("ITEM_SWIPED_IND"),
            col("item.drop_ship_ind").alias("DROP_SHIP_IND"),
            col("item.dept").cast(IntegerType()).alias("DEPT"),
            col("item.class").cast(IntegerType()).alias("CLASS"),
            col("item.subclass").cast(IntegerType()).alias("SUBCLASS"),
            col("item.qty").cast(DecimalType(12, 4)).alias("QTY"),
            col("item.unit_retail").cast(DecimalType(20, 4)).alias("UNIT_RETAIL"),
            col("item.selling_uom").alias("SELLING_UOM"),
            col("item.uom_quantity").cast(DecimalType(12, 4)).alias("UOM_QUANTITY"),
            col("item.orig_unit_retail").cast(DecimalType(20, 4)).alias("ORIG_UNIT_RETAIL"),
            col("item.override_reason").alias("OVERRIDE_REASON"),
            col("item.tax_ind").alias("TAX_IND"),
            col("item.unit_retail_vat_incl").alias("UNIT_RETAIL_VAT_INCL"),
            col("item.total_igtax_amt").cast(DecimalType(20, 4)).alias("TOTAL_IGTAX_AMT"),
            col("item.return_reason_code").alias("RETURN_REASON_CODE"),
            col("item.cust_order_no").alias("CUST_ORDER_NO"),
            col("item.error_ind").alias("ERROR_IND"),
        )
        .filter(
            col("STORE").isNotNull()
            & col("BUSINESS_DATE").isNotNull()
            & col("TRAN_SEQ_NO_NATURAL").isNotNull()   # part of surrogate
            & col("TRAN_DATETIME").isNotNull()         # part of surrogate
        )
    )

    # 3. Surrogate key — shared helper, same hash across every silver notebook
    keyed = flat.withColumn("TRAN_SEQ_NO", tran_seq_no_expr())

    # 4. FK enrich — shared helper. Single join key for tran-level child.
    enriched = enrich_with_parent_fx(keyed, PARENT_TABLE, ["TRAN_SEQ_NO"])

    # 5. Derive USD companions + lineage
    derived = (
        enriched
        .withColumn("UNIT_RETAIL_USD",
                    (col("UNIT_RETAIL") * col("FX_RATE")).cast(DecimalType(20, 4)))
        .withColumn("ORIG_UNIT_RETAIL_USD",
                    (col("ORIG_UNIT_RETAIL") * col("FX_RATE")).cast(DecimalType(20, 4)))
        .withColumn("TOTAL_IGTAX_AMT_USD",
                    (col("TOTAL_IGTAX_AMT") * col("FX_RATE")).cast(DecimalType(20, 4)))
        .withColumn("_silver_ts", current_timestamp())
        .withColumn("_source",    lit(SOURCE_TABLE))
    )

    # 6. DQ split
    dq = derived.withColumn(
        "rejection_reason",
        array_compact(array(
            when(~col("_has_parent"),
                 lit("orphan_no_parent_header (TRAN_SEQ_NO not in sa_tran_head)")),
            when(col("ITEM_SEQ_NO").isNull(),
                 lit("ITEM_SEQ_NO null — cannot form PK")),
            when(col("ITEM_TYPE").isNull(),
                 lit("ITEM_TYPE null — mandatory in ReSA")),
        )),
    )

    clean   = dq.filter("size(rejection_reason) = 0").drop("rejection_reason", "_has_parent")
    rejects = dq.filter("size(rejection_reason) > 0").drop("_has_parent")
    # Note: _quarantine_ts is added inside merge_and_quarantine — do NOT add here.

    # Project clean to exact target schema order
    clean = clean.select(*[f.name for f in sa_tran_item_schema.fields])

    # 7. MERGE clean + append rejects — both via shared helper
    clean_n, reject_n = merge_and_quarantine(
        clean_df=clean,
        rejects_df=rejects,
        target_table=TARGET_TABLE,
        quarantine_table=QUARANTINE_TABLE,
        merge_keys=["TRAN_SEQ_NO", "ITEM_SEQ_NO"],
    )

    print(f"Batch {batch_id}: clean={clean_n} rejects={reject_n}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run the streaming merge

# COMMAND ----------

(
    spark.readStream
    .table(SOURCE_TABLE)
    .writeStream
    .foreachBatch(merge_microbatch)
    .trigger(availableNow=True)
    .option("checkpointLocation", CHECKPOINT_PATH)
    .start()
    .awaitTermination()
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Validation + diagnostics

# COMMAND ----------

# Row counts
silver_count = spark.table(TARGET_TABLE).count()
print(f"silver.sa_tran_item row count: {silver_count}")

if spark.catalog.tableExists(QUARANTINE_TABLE):
    q_count = spark.table(QUARANTINE_TABLE).count()
    print(f"quarantine row count:          {q_count}")
    if q_count > 0:
        print("\nTop rejection reasons:")
        (
            spark.table(QUARANTINE_TABLE)
            .selectExpr("explode(rejection_reason) as reason")
            .groupBy("reason").count()
            .orderBy(col("count").desc())
            .show(truncate=False)
        )
else:
    print("quarantine row count:          0 (table not created)")

# Validation checks (only when target has rows)
if silver_count > 0:
    # PK uniqueness on (TRAN_SEQ_NO, ITEM_SEQ_NO)
    dup_count = (
        spark.table(TARGET_TABLE)
        .groupBy("TRAN_SEQ_NO", "ITEM_SEQ_NO").count()
        .where("count > 1").count()
    )
    assert dup_count == 0, f"PK violation: {dup_count} duplicate (TRAN_SEQ_NO, ITEM_SEQ_NO)"
    print("PK uniqueness check passed")

    # FK integrity — every item's TRAN_SEQ_NO must exist in the parent (0 orphans post-quarantine)
    orphans = (
        spark.table(TARGET_TABLE).select("TRAN_SEQ_NO").distinct().alias("i")
        .join(
            spark.table(PARENT_TABLE).select("TRAN_SEQ_NO").alias("h"),
            on="TRAN_SEQ_NO", how="left_anti",
        )
        .count()
    )
    assert orphans == 0, f"FK violation: {orphans} item TRAN_SEQ_NO values with no parent header"
    print("FK integrity check passed (0 orphans)")

    # Distribution diagnostics
    print("\n=== ITEM_TYPE distribution ===")
    spark.table(TARGET_TABLE).groupBy("ITEM_TYPE").count().orderBy(col("count").desc()).show()

    print("=== Items per transaction (fan-out distribution) ===")
    (
        spark.table(TARGET_TABLE)
        .groupBy("TRAN_SEQ_NO").count()
        .withColumnRenamed("count", "items_in_tran")     # rename BEFORE second groupBy to avoid collision
        .groupBy("items_in_tran").count()
        .withColumnRenamed("count", "transactions")
        .orderBy("items_in_tran")
        .show()
    )

    print("=== Channel distribution (sanity check — should all be POS in Pass-1) ===")
    spark.table(TARGET_TABLE).groupBy("RTLOG_ORIG_SYS").count().show()

    print("=== FX sanity (first 10 ITM lines) ===")
    (
        spark.table(TARGET_TABLE)
        .where("ITEM_TYPE = 'ITM'")
        .select("STORE", "TRAN_SEQ_NO", "ITEM", "QTY", "UNIT_RETAIL",
                "CURRENCY_CODE", "FX_RATE", "UNIT_RETAIL_USD")
        .limit(10)
        .show(truncate=False)
    )
