# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_tran_head` (Marketplace)
# MAGIC
# MAGIC The marketplace conformance path. One row per marketplace order, written to the
# MAGIC same `silver.sa_tran_head` table as POS, distinguished by `RTLOG_ORIG_SYS='MKT'`.
# MAGIC This is the first time two channels share a silver target — the surrogate-key hash's
# MAGIC channel-first design pays off here (cross-channel collisions are structurally
# MAGIC impossible).
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source** | `retaildp.bronze.marketplace` (7 marketplaces, 5 currencies) |
# MAGIC | **Target** | `retaildp.silver.sa_tran_head` (shared with POS) |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_tran_head_marketplace_rejects` (per-source) |
# MAGIC | **FK lookups** | `silver.sa_store_day`, `silver.sa_store_data`, `bronze.fx_rates` |
# MAGIC | **Pattern** | `readStream` + `availableNow` + `foreachBatch` → MERGE |
# MAGIC | **Idempotent** | Yes — deterministic `TRAN_SEQ_NO` + MERGE on `TRAN_SEQ_NO` |
# MAGIC | **Partitioned by** | `BUSINESS_DATE` (inherited from target) |
# MAGIC
# MAGIC ## Shared helpers used (see `_shared/`)
# MAGIC - `surrogate_keys.tran_seq_no_expr()` — same canonical hash as POS
# MAGIC - `quarantine.merge_and_quarantine()` — idempotent MERGE + quarantine append
# MAGIC
# MAGIC *Note:* same exclusions as `sa_tran_head_pos.py` — `fx_helpers` is not used (we derive
# MAGIC FX from `bronze.fx_rates` directly, as the parent), `schema_gate` not needed
# MAGIC (`bronze.marketplace` is a flat per-order row, not an optional array).
# MAGIC
# MAGIC ## Locked decisions (Pass-2 design choices)
# MAGIC 1. **`BUSINESS_DATE = settle_date`** — when money moved, matches POS's till-close semantics.
# MAGIC    `order_date` is preserved indirectly via `TRAN_SEQ_NO_NATURAL = order_id` (the prefix
# MAGIC    encodes the order date) and can be recovered if needed.
# MAGIC 2. **Status → TRAN_TYPE mapping** — introduces a new ReSA-extension type:
# MAGIC    - `DELIVERED        → SALE`
# MAGIC    - `RETURNED         → RETURN`
# MAGIC    - `CANCELLED_REFUND → CREFUND`  ← new type for "cancelled after settlement, money refunded"
# MAGIC
# MAGIC    `CREFUND` is distinct from `RETURN`: a return is a customer-initiated send-back of
# MAGIC    a delivered item; a cancellation-refund is an order voided after payment was already
# MAGIC    processed (e.g. seller out of stock, customer cancellation post-charge). Different
# MAGIC    operational story, even if the cash effect is similar.
# MAGIC 3. **Item-level negative quantities are flipped to positive** in `02_sa_tran_item/marketplace.py`.
# MAGIC    Direction lives on the header's `TRAN_TYPE`, matching POS convention. The header's `VALUE`
# MAGIC    is already non-negative in bronze (the simulator clamps it ≥ 0), so no flip needed here.
# MAGIC 4. **`MARKETPLACE` captured in `REF_NO1`** — no schema change. `REF_NO2` holds `customer_id`
# MAGIC    so both marketplace-specific dimensions have a home without touching the table definition.
# MAGIC 5. **`REGISTER = 'ECOM'`** sentinel — schema requires NOT NULL, online has no till concept.
# MAGIC    Doubles as a column-level distinguisher between POS (real till IDs) and MKT (`ECOM`).
# MAGIC 6. **`TRAN_NO` parsed from `order_id`** — format is `{MARKETPLACE}-{YYYYMMDD}-{NNNNNN}`,
# MAGIC    so `regexp_extract(order_id, r"-(\d+)$", 1)` gives a stable Long. Parse failures go
# MAGIC    to quarantine (not silently dropped) since `TRAN_NO` is NOT NULL in the schema.
# MAGIC
# MAGIC ## Patterns introduced here (vs `sa_tran_head_pos.py`)
# MAGIC 1. **Semantic conformance, not flatten** — POS bronze → silver was mostly rename + cast.
# MAGIC    MKT requires *decisions*: status→type, settle vs order date, sentinel REGISTER, parsed
# MAGIC    TRAN_NO. This is the medallion value proposition: the silver layer is where source
# MAGIC    semantics get unified.
# MAGIC 2. **Same target table, channel-discriminated** — both notebooks write to `silver.sa_tran_head`.
# MAGIC    The channel-aware surrogate (`xxhash64(RTLOG_ORIG_SYS, …)`) makes collisions impossible.
# MAGIC    Validation cell asserts `POS ∩ MKT = ∅`.
# MAGIC 3. **Per-source quarantine** — `…silver_sa_tran_head_marketplace_rejects` rather than
# MAGIC    sharing a quarantine table with POS. Easier to grep for MKT-specific issues during
# MAGIC    debugging; both still queryable in unison via `silver_sa_tran_head_*_rejects`.
# MAGIC 4. **Multi-currency exercised for real** — Pass-1 was IND-only (one currency, one FX rate).
# MAGIC    MKT spans INR/USD/GBP/AED/SGD across 7 marketplaces, so the FX lookup actually varies
# MAGIC    row-to-row for the first time.
# MAGIC 5. **REF_NO1/REF_NO2 as extension columns** — keeps the schema stable across sources
# MAGIC    while still capturing source-specific dimensions. Demonstrates evolution discipline.
# MAGIC 6. **No bootstrap** — table exists from POS run. This notebook asserts its existence at
# MAGIC    the top and projects to the existing column list.
# MAGIC
# MAGIC ## DQ rules (failures routed to quarantine)
# MAGIC 1. `TRAN_DATETIME` parses from `settle_date`
# MAGIC 2. `TRAN_NO` parses from `order_id` trailing serial
# MAGIC 3. `TRAN_TYPE` ∈ {SALE, RETURN, CREFUND} (status mapping succeeded)
# MAGIC 4. `VALUE` is NOT NULL (`total_amt` present in bronze)
# MAGIC 5. `STORE_DAY_SEQ_NO` FK lookup succeeds against `sa_store_day`
# MAGIC 6. `CURRENCY_CODE` FK lookup succeeds against `sa_store_data`
# MAGIC 7. `TAX_MODE` derivable from `COUNTRY`
# MAGIC
# MAGIC ## NOT a DQ failure (kept as data, not quarantined)
# MAGIC - `bronze.currency != sa_store_data.CURRENCY_CODE` — legitimate for cross-border
# MAGIC   marketplaces. Diagnostic counts the drift but doesn't quarantine.
# MAGIC - `FX_RATE` null → `VALUE_USD` null (only if `fx_rates` has a gap)
# MAGIC - Empty `items[]` array — handled in `02_marketplace`; the header still lands here.
# MAGIC - `commission_*` fields not represented in silver — seller-side fee, not customer-side.
# MAGIC   Belongs in gold, not silver. Currently dropped.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, current_timestamp, lit, when, broadcast,
    array, array_compact, regexp_extract,
)
from pyspark.sql.types import (
    StructType, StructField, LongType, IntegerType, StringType,
    DateType, TimestampType, DecimalType, ArrayType,
)

dbutils.widgets.text("source_table", "retaildp.bronze.marketplace", "Source Bronze Table")
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
QUARANTINE_TABLE   = "retaildp.quarantine.silver_sa_tran_head_marketplace_rejects"
STORE_DAY_TABLE    = "retaildp.silver.sa_store_day"
STORE_MASTER_TABLE = "retaildp.silver.sa_store_data"
FX_TABLE           = "retaildp.bronze.fx_rates"
CHECKPOINT_PATH    = (
    "abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/silver/sa_tran_head/marketplace/"
)

# Marketplace-local valid TRAN_TYPE set. The full silver superset is wider (includes POS's
# PVOID/PAIDIN/PAIDOUT/NOSALE/OPEN/CLOSE); we validate MKT-channel rows against MKT's subset.
VALID_MKT_TRAN_TYPES = {"SALE", "RETURN", "CREFUND"}

# Sentinel for required NOT NULL columns that have no marketplace equivalent
ECOM_REGISTER_SENTINEL = "ECOM"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema — reused from existing table
# MAGIC
# MAGIC This notebook is an **additive writer**. The target table was bootstrapped by
# MAGIC `01_sa_tran_head/sa_tran_head_pos.py`. We don't redefine the schema here — we read
# MAGIC the column list from the existing table at runtime and project to it. That keeps
# MAGIC the two notebooks in lockstep without duplicating the StructType definition.

# COMMAND ----------

assert spark.catalog.tableExists(TARGET_TABLE), (
    f"{TARGET_TABLE} does not exist. Run 01_sa_tran_head/sa_tran_head_pos.py first "
    "to bootstrap the table; this notebook is an additive writer for the MKT channel."
)

target_schema     = spark.table(TARGET_TABLE).schema
TARGET_COLUMNS    = [f.name for f in target_schema.fields]
quarantine_schema = StructType(target_schema.fields + [
    StructField("rejection_reason", ArrayType(StringType()), nullable=False),
    StructField("_quarantine_ts",   TimestampType(),         nullable=False),
])

print(f"Target schema has {len(TARGET_COLUMNS)} columns.")
print(f"Quarantine schema will be target + rejection_reason + _quarantine_ts.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## foreachBatch handler
# MAGIC
# MAGIC Per micro-batch:
# MAGIC 1. **Flatten + conform** — top-level bronze fields (no nested struct unlike POS),
# MAGIC    plus the semantic transforms locked above (status→type, settle→business_date,
# MAGIC    sentinel REGISTER, parsed TRAN_NO).
# MAGIC 2. **Surrogate key** — `tran_seq_no_expr()` from `_shared/surrogate_keys`. Channel
# MAGIC    discriminator `RTLOG_ORIG_SYS='MKT'` in the hash makes cross-source collisions
# MAGIC    impossible.
# MAGIC 3. **Dimension joins** — `sa_store_data` (currency, country) and `sa_store_day`
# MAGIC    (store-day spine). Same shape as POS notebook.
# MAGIC 4. **FX enrich** — broadcast join `bronze.fx_rates` on (business_date, currency).
# MAGIC 5. **Derive** — `TAX_MODE` from country, `VALUE_USD = VALUE * FX_RATE`, plus explicit
# MAGIC    nulls for the ReSA columns marketplace doesn't populate (BANNER_NO, CASHIER, etc.).
# MAGIC 6. **DQ split** — clean vs reject via `rejection_reason` array.
# MAGIC 7. **Write** — `merge_and_quarantine()`. Idempotent MERGE on `TRAN_SEQ_NO` + append
# MAGIC    to quarantine.

# COMMAND ----------

def merge_microbatch(microBatchDF: DataFrame, batch_id: int) -> None:
    # 1. Flatten bronze.marketplace + apply locked semantic conformance
    flat = (
        microBatchDF
        .select(
            col("rtlog_orig_sys").alias("RTLOG_ORIG_SYS"),
            col("store_no").cast(LongType()).alias("STORE"),

            # DECISION 1: BUSINESS_DATE = settle_date (not order_date)
            col("settle_date").cast(DateType()).alias("BUSINESS_DATE"),
            # TRAN_DATETIME stabilises the surrogate hash. Use settle_date midnight
            # since marketplace doesn't emit a wall-clock timestamp.
            col("settle_date").cast(TimestampType()).alias("TRAN_DATETIME"),

            # Natural composite for the surrogate. order_id is globally unique by
            # simulator construction: {MARKETPLACE}-{YYYYMMDD}-{NNNNNN}.
            col("order_id").alias("TRAN_SEQ_NO_NATURAL"),

            # DECISION 5: REGISTER sentinel — schema requires NOT NULL.
            lit(ECOM_REGISTER_SENTINEL).alias("REGISTER"),

            # DECISION 6: TRAN_NO parsed from trailing -NNNNNN. Null on parse failure → DQ rejects.
            regexp_extract(col("order_id"), r"-(\d+)$", 1).cast(LongType()).alias("TRAN_NO"),

            # DECISION 2: status → TRAN_TYPE. Unknown statuses produce NULL → DQ rejects.
            when(col("status") == "DELIVERED",         lit("SALE"))
            .when(col("status") == "RETURNED",         lit("RETURN"))
            .when(col("status") == "CANCELLED_REFUND", lit("CREFUND"))
            .otherwise(lit(None).cast(StringType()))
            .alias("TRAN_TYPE"),

            # Preserve the raw status string for traceability
            col("status").alias("STATUS"),

            # VALUE = order total. Already clamped non-negative in the simulator.
            col("total_amt").cast(DecimalType(20, 4)).alias("VALUE"),

            # DECISION 4: marketplace + customer in REF_NO1 / REF_NO2 (no schema change)
            col("marketplace").alias("REF_NO1"),
            col("customer_id").alias("REF_NO2"),

            # Bronze-emitted currency. Kept here for the multi-currency drift diagnostic
            # later; the authoritative CURRENCY_CODE comes from sa_store_data via FK.
            col("currency").alias("_bronze_CURRENCY"),
        )
        .filter(
            col("STORE").isNotNull()
            & col("BUSINESS_DATE").isNotNull()
            & col("TRAN_SEQ_NO_NATURAL").isNotNull()
            & col("TRAN_DATETIME").isNotNull()
        )
    )

    # 2. Surrogate — shared helper. Channel-first hash → no collisions vs POS.
    keyed = flat.withColumn("TRAN_SEQ_NO", tran_seq_no_expr())

    # 3a. sa_store_data → CURRENCY_CODE, COUNTRY
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

    # 3b. sa_store_day → STORE_DAY_SEQ_NO
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

    # 4. FX enrich — same logic as POS notebook
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

    # 5. Derive TAX_MODE, VALUE_USD, lineage. Explicit NULLs for ReSA columns absent in MKT.
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
        # MKT bronze has no equivalent for these — explicit NULLs to match target schema
        .withColumn("SUB_TRAN_TYPE",   lit(None).cast(StringType()))
        .withColumn("BANNER_NO",       lit(None).cast(IntegerType()))
        .withColumn("CASHIER",         lit(None).cast(StringType()))
        .withColumn("SALESPERSON",     lit(None).cast(StringType()))
        .withColumn("POS_TRAN_IND",    lit("N"))   # marketplace is not a POS tran
        .withColumn("ERROR_IND",       lit(None).cast(StringType()))
        .withColumn("ORIG_TRAN_NO",    lit(None).cast(StringType()))
        .withColumn("ORIG_TRAN_TYPE",  lit(None).cast(StringType()))
        .withColumn("ORIG_REG_NO",     lit(None).cast(StringType()))
        .withColumn("REV_NO",          lit(None).cast(IntegerType()))
        .withColumn("REASON_CODE",     lit(None).cast(StringType()))
        .withColumn("VENDOR_NO",       lit(None).cast(StringType()))
        .withColumn("VENDOR_INVC_NO",  lit(None).cast(StringType()))
        .withColumn("UPDATE_DATETIME", lit(None).cast(TimestampType()))
        .withColumn("UPDATE_ID",       lit(None).cast(StringType()))
        # Lineage
        .withColumn("_silver_ts",      current_timestamp())
        .withColumn("_source",         lit(SOURCE_TABLE))
    )

    # 6. DQ split — MKT-specific rejection reasons
    dq = derived.withColumn(
        "rejection_reason",
        array_compact(array(
            when(col("TRAN_DATETIME").isNull(),
                 lit("TRAN_DATETIME could not be parsed from settle_date")),
            when(col("TRAN_NO").isNull(),
                 lit("TRAN_NO could not be parsed from order_id (expected ...-NNNNNN suffix)")),
            when(~col("TRAN_TYPE").isin(*VALID_MKT_TRAN_TYPES),
                 lit("TRAN_TYPE not in MKT-valid set {SALE, RETURN, CREFUND}; status mapping failed")),
            when(col("VALUE").isNull(),
                 lit("VALUE must be NOT NULL (total_amt missing)")),
            when(col("STORE_DAY_SEQ_NO").isNull(),
                 lit("STORE_DAY_SEQ_NO FK lookup failed (no matching sa_store_day row)")),
            when(col("CURRENCY_CODE").isNull(),
                 lit("CURRENCY_CODE FK lookup failed (store missing in sa_store_data)")),
            when(col("TAX_MODE").isNull(),
                 lit("TAX_MODE could not be derived (unknown COUNTRY)")),
        )),
    )

    # Drop the bronze-currency helper before write
    clean   = dq.filter("size(rejection_reason) = 0").drop("rejection_reason", "_bronze_CURRENCY")
    rejects = dq.filter("size(rejection_reason) > 0").drop("_bronze_CURRENCY")
    # _quarantine_ts added inside merge_and_quarantine

    # Project clean to exact target schema order (sourced from existing table)
    clean = clean.select(*TARGET_COLUMNS)

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
# MAGIC ## Validation + diagnostics (channel-filtered to MKT)

# COMMAND ----------

total_mkt   = spark.table(TARGET_TABLE).where("RTLOG_ORIG_SYS = 'MKT'").count()
total_pos   = spark.table(TARGET_TABLE).where("RTLOG_ORIG_SYS = 'POS'").count()
total_all   = spark.table(TARGET_TABLE).count()
print(f"silver.sa_tran_head MKT row count:    {total_mkt:,}")
print(f"silver.sa_tran_head POS row count:    {total_pos:,}")
print(f"silver.sa_tran_head total row count:  {total_all:,}")

if spark.catalog.tableExists(QUARANTINE_TABLE):
    q_count = spark.table(QUARANTINE_TABLE).count()
    print(f"MKT quarantine row count:             {q_count:,}")
    if q_count > 0:
        print("\nTop MKT rejection reasons:")
        (
            spark.table(QUARANTINE_TABLE)
            .selectExpr("explode(rejection_reason) as reason")
            .groupBy("reason").count()
            .orderBy(col("count").desc())
            .show(truncate=False)
        )
else:
    print("MKT quarantine row count:             0 (table not created)")

if total_mkt > 0:
    mkt = spark.table(TARGET_TABLE).where("RTLOG_ORIG_SYS = 'MKT'")

    # PK uniqueness within MKT
    dup_count = mkt.groupBy("TRAN_SEQ_NO").count().where("count > 1").count()
    assert dup_count == 0, f"PK violation: {dup_count} duplicate TRAN_SEQ_NO within MKT"
    print("PK uniqueness check passed (within MKT)")

    # CROSS-CHANNEL COLLISION CHECK — the payoff for the channel-aware surrogate.
    # POS and MKT TRAN_SEQ_NO sets must be disjoint.
    collisions = (
        spark.table(TARGET_TABLE).where("RTLOG_ORIG_SYS = 'MKT'").select("TRAN_SEQ_NO").alias("m")
        .join(
            spark.table(TARGET_TABLE).where("RTLOG_ORIG_SYS = 'POS'").select("TRAN_SEQ_NO").alias("p"),
            on="TRAN_SEQ_NO", how="inner",
        ).count()
    )
    assert collisions == 0, f"Cross-channel collision: {collisions} TRAN_SEQ_NO values exist in BOTH POS and MKT"
    print(f"Cross-channel collision check passed (POS ∩ MKT = ∅; {total_pos:,} POS + {total_mkt:,} MKT = {total_pos+total_mkt:,})")

    # MKT-channel domain checks (filtered, doesn't trip on POS-only types like NOSALE/OPEN/CLOSE)
    bad_type = mkt.where(~col("TRAN_TYPE").isin(*VALID_MKT_TRAN_TYPES)).count()
    assert bad_type == 0, f"{bad_type} MKT rows with invalid TRAN_TYPE"
    print("TRAN_TYPE domain check passed (MKT-channel filtered)")

    bad_tax = mkt.where(~col("TAX_MODE").isin("IGTAX", "TAX", "BOTH")).count()
    assert bad_tax == 0, f"{bad_tax} MKT rows with invalid TAX_MODE"
    print("TAX_MODE domain check passed (MKT)")

    # REGISTER sentinel check
    bad_register = mkt.where(col("REGISTER") != ECOM_REGISTER_SENTINEL).count()
    assert bad_register == 0, f"{bad_register} MKT rows with REGISTER != 'ECOM' (sentinel violated)"
    print("REGISTER sentinel check passed (all MKT rows have REGISTER='ECOM')")

    # Distribution diagnostics
    print("\n=== MKT TRAN_TYPE distribution (status → type mapping) ===")
    mkt.groupBy("TRAN_TYPE", "STATUS").count().orderBy(col("count").desc()).show()

    print("=== MKT marketplace distribution (stored in REF_NO1) ===")
    mkt.groupBy("REF_NO1").count().orderBy(col("count").desc()).show()

    print("=== MKT country + currency + tax mode distribution ===")
    mkt.groupBy("COUNTRY", "CURRENCY_CODE", "TAX_MODE").count().orderBy(col("count").desc()).show()

    # Multi-currency drift diagnostic — bronze.currency vs silver.CURRENCY_CODE
    # By marketplace design (1 marketplace = 1 country = 1 currency), these should agree.
    # Drift > 0 means a store was assigned to a marketplace whose currency doesn't match
    # the store's country — worth investigating in stores.csv vs reference_data.py.
    drift_count = (
        spark.table(SOURCE_TABLE).alias("b")
        .join(
            mkt.alias("s"),
            col("b.order_id") == col("s.TRAN_SEQ_NO_NATURAL"),
            "inner",
        )
        .where(col("b.currency") != col("s.CURRENCY_CODE"))
        .count()
    )
    print(f"\n=== Multi-currency drift (bronze.currency != silver.CURRENCY_CODE): {drift_count} ===")
    print("    (expected 0 by simulator design; non-zero means store-marketplace currency mismatch)")

    print("\n=== FX sanity — first 10 SALE rows across marketplaces ===")
    (
        mkt
        .where("TRAN_TYPE = 'SALE'")
        .select("STORE", "BUSINESS_DATE", "REF_NO1", "VALUE", "CURRENCY_CODE", "FX_RATE", "VALUE_USD")
        .limit(10)
        .show(truncate=False)
    )

    print("=== A few CREFUND rows (the new TRAN_TYPE) ===")
    (
        mkt
        .where("TRAN_TYPE = 'CREFUND'")
        .select("STORE", "BUSINESS_DATE", "REF_NO1", "VALUE", "STATUS", "CURRENCY_CODE", "VALUE_USD")
        .limit(5)
        .show(truncate=False)
    )
else:
    print("\nNo MKT rows landed. Check bronze.marketplace has data and rerun.")
