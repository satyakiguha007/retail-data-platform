# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_tran_head` (POS)
# MAGIC
# MAGIC The central transactional table. One row per transaction, conformed to the
# MAGIC ReSA-canonical `SA_TRAN_HEAD` schema. Every `sa_tran_*` child table joins back here.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source** | `retaildp.bronze.pos_rtlog` |
# MAGIC | **Target** | `retaildp.silver.sa_tran_head` |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_tran_head_rejects` |
# MAGIC | **FK lookups** | `silver.sa_store_day`, `silver.sa_store_data`, `bronze.fx_rates` |
# MAGIC | **Pattern** | `readStream` + `availableNow` + `foreachBatch` → MERGE |
# MAGIC | **Idempotent** | Yes — deterministic `TRAN_SEQ_NO` + MERGE |
# MAGIC | **Partitioned by** | `BUSINESS_DATE` |
# MAGIC
# MAGIC ## Shared helpers used (see `_shared/`)
# MAGIC - `surrogate_keys.tran_seq_no_expr()` — canonical `TRAN_SEQ_NO` hash (cell 6 step 2)
# MAGIC - `quarantine.merge_and_quarantine()` — idempotent MERGE + quarantine append (cell 6 step 7)
# MAGIC
# MAGIC *Note:* `fx_helpers.enrich_with_parent_fx` is NOT used here — `01` is the parent. It derives
# MAGIC `FX_RATE` from `bronze.fx_rates` directly (step 4), which is the foundational FX lookup that
# MAGIC all child tables then inherit. `schema_gate` is not needed either — `tran_head` is a non-array
# MAGIC struct present on every bronze row.
# MAGIC
# MAGIC ## Five new patterns introduced here (the foundation for the rest of silver)
# MAGIC 1. **Surrogate from source ID + datetime tie-breaker** — `TRAN_SEQ_NO = xxhash64(rtlog_orig_sys, tran_seq_no_natural, tran_datetime)`. Module 1's fault injection makes both the natural composite `(store, date, register, tran_no)` AND the simulator's own `tran_seq_no` non-unique (the injector regenerates / clones records). `tran_datetime` is the reliable tie-breaker — two real events can't share a till at the same microsecond.
# MAGIC 2. **Multiple FK joins** — `sa_store_day` (store-day spine), `sa_store_data` (currency, country)
# MAGIC 3. **FX normalisation** — broadcast join `bronze.fx_rates` on `(business_date, currency)` → `VALUE_USD`
# MAGIC 4. **Channel conformance hook** — `RTLOG_ORIG_SYS` column is the channel discriminator; Pass-2/3 add MKT and Olist via UNION above the conformance block
# MAGIC 5. **Tax mode inference** — IND → IGTAX, USA/GBR → TAX, ARE/SGP → BOTH
# MAGIC
# MAGIC ## DQ rules (failures routed to quarantine)
# MAGIC 1. `TRAN_DATETIME` parses to a valid timestamp
# MAGIC 2. `TRAN_TYPE` in {SALE, RETURN, PVOID, PAIDIN, PAIDOUT, NOSALE, OPEN, CLOSE}
# MAGIC 3. `VALUE` is NOT NULL (zero is fine — OPEN/CLOSE/NOSALE legitimately have value=0)
# MAGIC 4. `STORE_DAY_SEQ_NO` FK lookup succeeds against `sa_store_day`
# MAGIC 5. `CURRENCY_CODE` FK lookup succeeds against `sa_store_data`
# MAGIC 6. `TAX_MODE` resolvable from `COUNTRY`
# MAGIC
# MAGIC ## NOT a DQ failure (kept as data, not quarantined)
# MAGIC - `ERROR_IND = 'Y'` — fault-injected rows pass through; Module 4 audit catches these via `SA_ERROR`
# MAGIC - `FX_RATE` null → `VALUE_USD` null → row still lands (rare; only if fx_rates has gaps)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, current_timestamp, lit, when, broadcast,
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

# COMMAND ----------

# MAGIC %run ../_shared/surrogate_keys

# COMMAND ----------

# MAGIC %run ../_shared/quarantine

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

TARGET_TABLE       = "retaildp.silver.sa_tran_head"
QUARANTINE_TABLE   = "retaildp.quarantine.silver_sa_tran_head_rejects"
STORE_DAY_TABLE    = "retaildp.silver.sa_store_day"
STORE_MASTER_TABLE = "retaildp.silver.sa_store_data"
FX_TABLE           = "retaildp.bronze.fx_rates"
CHECKPOINT_PATH    = (
    "abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/silver/sa_tran_head/"
)

# Valid sets
VALID_TRAN_TYPES = {"SALE", "RETURN", "PVOID", "PAIDIN", "PAIDOUT", "NOSALE", "OPEN", "CLOSE"}

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema
# MAGIC
# MAGIC ReSA-canonical column names + a few lakehouse additions (`VALUE_USD`, `FX_RATE`, `TAX_MODE`, `COUNTRY`).
# MAGIC Lineage columns prefixed with underscore.

# COMMAND ----------

sa_tran_head_schema = StructType([
    # Identity & FKs
    StructField("TRAN_SEQ_NO",         LongType(),        nullable=False),  # surrogate (xxhash64)
    StructField("STORE_DAY_SEQ_NO",    LongType(),        nullable=False),  # FK → sa_store_day
    StructField("RTLOG_ORIG_SYS",      StringType(),      nullable=False),  # POS / MKT / OLIST

    # Natural composite (kept for debuggability + ReSA fidelity)
    StructField("STORE",               LongType(),        nullable=False),
    StructField("BUSINESS_DATE",       DateType(),        nullable=False),  # partition
    StructField("REGISTER",            StringType(),      nullable=False),
    StructField("TRAN_NO",             LongType(),        nullable=False),

    # Transaction attributes
    StructField("TRAN_SEQ_NO_NATURAL", StringType(),      nullable=True),   # source composite string
    StructField("TRAN_DATETIME",       TimestampType(),   nullable=False),
    StructField("TRAN_TYPE",           StringType(),      nullable=False),
    StructField("SUB_TRAN_TYPE",       StringType(),      nullable=True),
    StructField("STATUS",              StringType(),      nullable=False),

    # Amounts + FX
    StructField("VALUE",               DecimalType(20,4), nullable=False),  # local currency
    StructField("CURRENCY_CODE",       StringType(),      nullable=False),  # from sa_store_data
    StructField("FX_RATE",             DecimalType(20,6), nullable=True),
    StructField("VALUE_USD",           DecimalType(20,4), nullable=True),   # VALUE * FX_RATE

    # Country-driven
    StructField("COUNTRY",             StringType(),      nullable=False),  # ISO3
    StructField("TAX_MODE",            StringType(),      nullable=False),  # IGTAX/TAX/BOTH

    # People + flags
    StructField("BANNER_NO",           IntegerType(),     nullable=True),
    StructField("CASHIER",             StringType(),      nullable=True),
    StructField("SALESPERSON",         StringType(),      nullable=True),
    StructField("POS_TRAN_IND",        StringType(),      nullable=True),
    StructField("ERROR_IND",           StringType(),      nullable=True),

    # Reversal/refund refs
    StructField("ORIG_TRAN_NO",        StringType(),      nullable=True),
    StructField("ORIG_TRAN_TYPE",      StringType(),      nullable=True),
    StructField("ORIG_REG_NO",         StringType(),      nullable=True),
    StructField("REV_NO",              IntegerType(),     nullable=True),
    StructField("REASON_CODE",         StringType(),      nullable=True),

    # Misc
    StructField("REF_NO1",             StringType(),      nullable=True),
    StructField("REF_NO2",             StringType(),      nullable=True),
    StructField("VENDOR_NO",           StringType(),      nullable=True),
    StructField("VENDOR_INVC_NO",      StringType(),      nullable=True),
    StructField("UPDATE_DATETIME",     TimestampType(),   nullable=True),
    StructField("UPDATE_ID",           StringType(),      nullable=True),

    # Lineage
    StructField("_silver_ts",          TimestampType(),   nullable=False),
    StructField("_source",             StringType(),      nullable=False),
])

# Quarantine = target columns + rejection_reason + quarantine_ts
quarantine_schema = StructType(
    sa_tran_head_schema.fields + [
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
        spark.createDataFrame([], sa_tran_head_schema).write
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
# MAGIC ## foreachBatch handler
# MAGIC
# MAGIC Per micro-batch:
# MAGIC 1. **Flatten** — pull tran_head struct fields + top-level columns into a flat shape with explicit aliases.
# MAGIC 2. **Surrogate key** — `tran_seq_no_expr()` from `_shared/surrogate_keys`. Same hash across every silver notebook.
# MAGIC 3. **FK enrich** — broadcast joins to `sa_store_data` (currency, country) and `sa_store_day` (store-day spine). Inline (these are dimension lookups, not the parent-FX pattern).
# MAGIC 4. **FX enrich** — broadcast join to `bronze.fx_rates` on `(business_date, currency)` → `FX_RATE`. Inline (this is where FX FIRST enters silver — children then inherit from sa_tran_head).
# MAGIC 5. **Derive** — `TAX_MODE` from country, `VALUE_USD = VALUE * FX_RATE`.
# MAGIC 6. **DQ split** — clean vs reject with `rejection_reason` array.
# MAGIC 7. **Write** — `merge_and_quarantine()` from `_shared/quarantine`. Idempotent MERGE on `TRAN_SEQ_NO` + append to quarantine.

# COMMAND ----------

def merge_microbatch(microBatchDF: DataFrame, batch_id: int) -> None:
    # 1. Flatten — explicit aliases for every column (avoids case-collision pitfalls)
    flat = (
        microBatchDF
        .select(
            col("rtlog_orig_sys").alias("RTLOG_ORIG_SYS"),
            col("store").cast(LongType()).alias("STORE"),
            col("date").alias("BUSINESS_DATE"),
            col("tran_head.register").alias("REGISTER"),
            col("tran_head.tran_no").cast(LongType()).alias("TRAN_NO"),
            col("tran_head.tran_seq_no").alias("TRAN_SEQ_NO_NATURAL"),
            col("tran_head.tran_datetime").cast(TimestampType()).alias("TRAN_DATETIME"),
            col("tran_head.tran_type").alias("TRAN_TYPE"),
            col("tran_head.sub_tran_type").alias("SUB_TRAN_TYPE"),
            col("tran_head.status").alias("STATUS"),
            col("tran_head.value").cast(DecimalType(20, 4)).alias("VALUE"),
            col("tran_head.banner_no").cast(IntegerType()).alias("BANNER_NO"),
            col("tran_head.cashier").alias("CASHIER"),
            col("tran_head.salesperson").alias("SALESPERSON"),
            col("tran_head.pos_tran_ind").alias("POS_TRAN_IND"),
            col("tran_head.error_ind").alias("ERROR_IND"),
            col("tran_head.orig_tran_no").alias("ORIG_TRAN_NO"),
            col("tran_head.orig_tran_type").alias("ORIG_TRAN_TYPE"),
            col("tran_head.orig_reg_no").alias("ORIG_REG_NO"),
            col("tran_head.rev_no").cast(IntegerType()).alias("REV_NO"),
            col("tran_head.reason_code").alias("REASON_CODE"),
            col("tran_head.ref_no1").alias("REF_NO1"),
            col("tran_head.ref_no2").alias("REF_NO2"),
            col("tran_head.vendor_no").alias("VENDOR_NO"),
            col("tran_head.vendor_invc_no").alias("VENDOR_INVC_NO"),
            col("tran_head.update_datetime").cast(TimestampType()).alias("UPDATE_DATETIME"),
            col("tran_head.update_id").alias("UPDATE_ID"),
        )
        .filter(
            col("STORE").isNotNull()
            & col("BUSINESS_DATE").isNotNull()
            & col("REGISTER").isNotNull()
            & col("TRAN_NO").isNotNull()
            & col("TRAN_SEQ_NO_NATURAL").isNotNull()   # part of surrogate
            & col("TRAN_DATETIME").isNotNull()         # part of surrogate
        )
    )

    # 2. Surrogate key — shared helper. Same hash across every silver notebook.
    keyed = flat.withColumn("TRAN_SEQ_NO", tran_seq_no_expr())

    # 3a. Enrich with sa_store_data → CURRENCY_CODE, COUNTRY
    store_data = (
        spark.table(STORE_MASTER_TABLE)
        .select(
            col("STORE").alias("_sd_STORE"),
            col("CURRENCY_CODE").alias("_sd_CURRENCY_CODE"),
            col("COUNTRY").alias("_sd_COUNTRY"),
        )
    )
    enriched_sd = (
        keyed
        .join(broadcast(store_data), col("STORE") == col("_sd_STORE"), "left")
        .withColumn("CURRENCY_CODE", col("_sd_CURRENCY_CODE"))
        .withColumn("COUNTRY",       col("_sd_COUNTRY"))
        .drop("_sd_STORE", "_sd_CURRENCY_CODE", "_sd_COUNTRY")
    )

    # 3b. Enrich with sa_store_day → STORE_DAY_SEQ_NO
    store_day = (
        spark.table(STORE_DAY_TABLE)
        .select(
            col("STORE").alias("_sday_STORE"),
            col("BUSINESS_DATE").alias("_sday_DATE"),
            col("STORE_DAY_SEQ_NO").alias("_sday_SEQ_NO"),
        )
    )
    enriched_sday = (
        enriched_sd
        .join(
            broadcast(store_day),
            (col("STORE") == col("_sday_STORE")) & (col("BUSINESS_DATE") == col("_sday_DATE")),
            "left",
        )
        .withColumn("STORE_DAY_SEQ_NO", col("_sday_SEQ_NO"))
        .drop("_sday_STORE", "_sday_DATE", "_sday_SEQ_NO")
    )

    # 4. FX enrich — broadcast bronze.fx_rates on (business_date, currency)
    fx = (
        spark.table(FX_TABLE)
        .filter(col("to_currency") == "USD")
        .select(
            col("rate_date").alias("_fx_DATE"),
            col("from_currency").alias("_fx_CURR"),
            col("rate").cast(DecimalType(20, 6)).alias("_fx_RATE"),
        )
    )
    enriched_fx = (
        enriched_sday
        .join(
            broadcast(fx),
            (col("BUSINESS_DATE") == col("_fx_DATE")) & (col("CURRENCY_CODE") == col("_fx_CURR")),
            "left",
        )
        .withColumn("FX_RATE", col("_fx_RATE"))
        .drop("_fx_DATE", "_fx_CURR", "_fx_RATE")
    )

    # 5. Derive TAX_MODE + VALUE_USD + lineage
    derived = (
        enriched_fx
        .withColumn(
            "TAX_MODE",
            when(col("COUNTRY") == "IND",                 lit("IGTAX"))
            .when(col("COUNTRY").isin("USA", "GBR"),      lit("TAX"))
            .when(col("COUNTRY").isin("ARE", "SGP"),      lit("BOTH"))
            .otherwise(lit(None).cast(StringType())),
        )
        .withColumn("VALUE_USD", (col("VALUE") * col("FX_RATE")).cast(DecimalType(20, 4)))
        .withColumn("_silver_ts", current_timestamp())
        .withColumn("_source",    lit(SOURCE_TABLE))
    )

    # 6. DQ split
    dq = derived.withColumn(
        "rejection_reason",
        array_compact(array(
            when(col("TRAN_DATETIME").isNull(),
                 lit("TRAN_DATETIME invalid or null")),
            when(~col("TRAN_TYPE").isin(*VALID_TRAN_TYPES),
                 lit("TRAN_TYPE not in valid set")),
            when(col("VALUE").isNull(),
                 lit("VALUE must be NOT NULL")),
            when(col("STORE_DAY_SEQ_NO").isNull(),
                 lit("STORE_DAY_SEQ_NO FK lookup failed (no matching sa_store_day row)")),
            when(col("CURRENCY_CODE").isNull(),
                 lit("CURRENCY_CODE FK lookup failed (store missing in sa_store_data)")),
            when(col("TAX_MODE").isNull(),
                 lit("TAX_MODE could not be derived (unknown COUNTRY)")),
        )),
    )

    clean   = dq.filter("size(rejection_reason) = 0").drop("rejection_reason")
    rejects = dq.filter("size(rejection_reason) > 0")
    # _quarantine_ts added by merge_and_quarantine

    # Project clean to exact target schema order
    clean = clean.select(*[f.name for f in sa_tran_head_schema.fields])

    # 7. MERGE clean + append rejects — shared helper
    clean_n, reject_n = merge_and_quarantine(
        clean_df=clean,
        rejects_df=rejects,
        target_table=TARGET_TABLE,
        quarantine_table=QUARANTINE_TABLE,
        merge_keys=["TRAN_SEQ_NO"],
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
print(f"silver.sa_tran_head row count: {silver_count}")

if spark.catalog.tableExists(QUARANTINE_TABLE):
    q_count = spark.table(QUARANTINE_TABLE).count()
    print(f"quarantine row count:           {q_count}")
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
    print("quarantine row count:           0 (table not created)")

# Validation checks (only when target has rows)
if silver_count > 0:
    # PK uniqueness
    dup_count = (
        spark.table(TARGET_TABLE)
        .groupBy("TRAN_SEQ_NO").count()
        .where("count > 1").count()
    )
    assert dup_count == 0, f"PK violation: {dup_count} duplicate TRAN_SEQ_NO"
    print("PK uniqueness check passed")

    # Domain checks
    bad_type = spark.table(TARGET_TABLE).where(~col("TRAN_TYPE").isin(*VALID_TRAN_TYPES)).count()
    assert bad_type == 0, f"{bad_type} rows with invalid TRAN_TYPE"
    print("TRAN_TYPE domain check passed")

    bad_tax = spark.table(TARGET_TABLE).where(~col("TAX_MODE").isin("IGTAX", "TAX", "BOTH")).count()
    assert bad_tax == 0, f"{bad_tax} rows with invalid TAX_MODE"
    print("TAX_MODE domain check passed")

    # Distribution diagnostics
    print("\n=== TRAN_TYPE distribution ===")
    spark.table(TARGET_TABLE).groupBy("TRAN_TYPE").count().orderBy(col("count").desc()).show()

    print("=== Channel distribution (sanity check — should all be POS in Pass-1) ===")
    spark.table(TARGET_TABLE).groupBy("RTLOG_ORIG_SYS").count().show()

    print("=== Country + tax mode distribution ===")
    spark.table(TARGET_TABLE).groupBy("COUNTRY", "TAX_MODE", "CURRENCY_CODE").count().show()

    print("=== FX sanity — VALUE vs VALUE_USD for first 10 SALE rows ===")
    (
        spark.table(TARGET_TABLE)
        .where("TRAN_TYPE = 'SALE'")
        .select("STORE", "BUSINESS_DATE", "VALUE", "CURRENCY_CODE", "FX_RATE", "VALUE_USD")
        .limit(10)
        .show()
    )
