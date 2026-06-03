# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — `sa_tran_tender` (Olist additive)
# MAGIC
# MAGIC Final Pass-3 notebook. Adds Olist payment rows to `sa_tran_tender`, with a 1:N
# MAGIC fan-out for installment payments and a 5-way `payment_type` → `TENDER_TYPE_GROUP`
# MAGIC mapping that includes the new `BOLETO` code and an `UNDEFINED` pass-through.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Source (payments)** | `retaildp.bronze.olist_order_payments` |
# MAGIC | **Source (orders)** | `retaildp.bronze.olist_orders` (for `order_purchase_timestamp` — surrogate input) |
# MAGIC | **Target** | `retaildp.silver.sa_tran_tender` (additive — POS bootstraps it) |
# MAGIC | **Quarantine** | `retaildp.quarantine.silver_sa_tran_tender_olist_rejects` |
# MAGIC | **Parent FK** | `silver.sa_tran_head` on `TRAN_SEQ_NO` |
# MAGIC | **Pattern** | Batch explode (1:N installment fan-out) → renumber → MERGE |
# MAGIC | **Idempotent** | Yes — deterministic `TRAN_SEQ_NO` + row_number renumbering |
# MAGIC | **Streaming** | No — Olist is static |
# MAGIC
# MAGIC ## What's new vs POS/MKT siblings
# MAGIC
# MAGIC **1:N fan-out via installments.** First notebook in the project that materialises
# MAGIC more silver rows than bronze rows for a tender. POS is 1:1 (each tender row in
# MAGIC bronze = one silver row). MKT synthesises one row per order. Olist's
# MAGIC `payment_installments` column is the cardinality multiplier — a `credit_card` row
# MAGIC with `payment_installments=4` and `payment_value=400.00` becomes **four** silver
# MAGIC rows, each with `TENDER_AMT=100.00`. This breaks the previous "1:1 header:tender"
# MAGIC invariant — Pass-3 introduces `1:N` semantics for the OMS channel.
# MAGIC
# MAGIC **TENDER_SEQ_NO is renumbered.** Bronze's `payment_sequential` enumerates payment
# MAGIC rows (1, 2, 3…), but after installment fan-out we have more silver rows than
# MAGIC bronze payment_sequentials, so we re-derive `TENDER_SEQ_NO` via `row_number()`
# MAGIC over `(payment_sequential ASC, installment_index ASC)` per order — gives clean
# MAGIC contiguous 1..N within every transaction.
# MAGIC
# MAGIC ## Locked decisions
# MAGIC
# MAGIC | # | Decision | Rationale |
# MAGIC |---|---|---|
# MAGIC | 1 | Installments > 1 → split into N silver rows, `TENDER_AMT = payment_value / N` | User-locked. Models the underlying customer cashflow (4 monthly charges of 100), not the headline amount |
# MAGIC | 2 | `payment_installments` clamped to `max(1, n)` before splitting | Olist has rare `installments=0` rows (mostly `not_defined`); clamping prevents `sequence(1, 0) = empty array` from dropping the row |
# MAGIC | 3 | `TENDER_SEQ_NO = row_number()` over (payment_sequential, installment_index) per order | Contiguous 1..N PK component; preserves bronze ordering as the primary sort key |
# MAGIC | 4 | `payment_type` mapping: credit_card→CREDIT, debit_card→DEBIT, voucher→VOUCHER, boleto→BOLETO, not_defined→UNDEFINED | Brief + user-locked; BOLETO is a new code (no POS/MKT analogue); UNDEFINED is a kept-but-flagged pass-through |
# MAGIC | 5 | Truly unknown `payment_type` (none of the 5 known values) → DQ reject | Defensive — catches future schema drift |
# MAGIC | 6 | `ORIG_CURRENCY` / `ORIG_CURR_AMT` NULL | Olist has no foreign-tender concept — all rows BRL; NULL is more honest than CURRENCY_CODE passthrough |
# MAGIC | 7 | `CC_NO`, `CC_AUTH_NO`, `CC_ENTRY_MODE`, `VOUCHER_NO` NULL | Olist bronze has none of these (privacy in source data) |
# MAGIC | 8 | Parent `TRAN_DATETIME` = `order_purchase_timestamp` | Must match `sa_tran_head_olist.py` exactly for FK alignment |
# MAGIC
# MAGIC ## DQ rules (failures routed to quarantine)
# MAGIC 1. Parent FK — `(TRAN_SEQ_NO,)` must resolve in `sa_tran_head` (orphan check via `_has_parent`)
# MAGIC 2. `TENDER_SEQ_NO` NOT NULL (PK component)
# MAGIC 3. `TENDER_TYPE_GROUP` NOT NULL — mapping failed (truly unknown `payment_type`)
# MAGIC 4. `TENDER_AMT` NOT NULL — `payment_value` was missing or installments split failed
# MAGIC
# MAGIC ## Known soft drift (NOT a DQ failure)
# MAGIC `head.VALUE` (rolled up from items: SUM price + freight) vs `SUM(TENDER_AMT)` per
# MAGIC order can drift by a few BRL in real Olist data — payment side may include
# MAGIC promotional adjustments / coupons not reflected in `olist_order_items.price`.
# MAGIC The validation block reports drift counts but does NOT assert. Hard reconciliation
# MAGIC is enforced item-side in `sa_tran_item_olist.py`.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports & widgets

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, current_timestamp, lit, when, broadcast,
    array, array_compact,
    sum as f_sum, explode, sequence, row_number, greatest,
)
from pyspark.sql.window import Window
from pyspark.sql.types import (
    StructType, StructField, LongType, IntegerType, StringType,
    DateType, TimestampType, DecimalType, ArrayType,
)

dbutils.widgets.text("payments_source", "retaildp.bronze.olist_order_payments", "Source Bronze Table (payments)")
dbutils.widgets.text("orders_source",   "retaildp.bronze.olist_orders",         "Source Bronze Table (orders, for parent ts)")
PAYMENTS_SOURCE = dbutils.widgets.get("payments_source")
ORDERS_SOURCE   = dbutils.widgets.get("orders_source")

SOURCE_TABLE = f"{PAYMENTS_SOURCE}+{ORDERS_SOURCE}"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Shared helpers

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

TARGET_TABLE     = "retaildp.silver.sa_tran_tender"
QUARANTINE_TABLE = "retaildp.quarantine.silver_sa_tran_tender_olist_rejects"
PARENT_TABLE     = "retaildp.silver.sa_tran_head"

OLIST_STORE = 99999

# Known payment_type → TENDER_TYPE_GROUP map. Truly unknown values (not in this dict)
# fall through to NULL and quarantine via DQ rule #3.
TENDER_TYPE_MAP = {
    "credit_card": "CREDIT",
    "debit_card":  "DEBIT",
    "voucher":     "VOUCHER",
    "boleto":      "BOLETO",
    "not_defined": "UNDEFINED",
}

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pre-flight diagnostics

# COMMAND ----------

print("=== sa_tran_head parent rows for OMS ===")
oms_head_count = (
    spark.table(PARENT_TABLE).where(col("RTLOG_ORIG_SYS") == "OMS").count()
)
if oms_head_count == 0:
    raise AssertionError(
        f"FATAL: no OMS rows in {PARENT_TABLE}. "
        "Run sa_tran_head_olist.py first; this notebook FK-joins to it."
    )
print(f"OK — {oms_head_count:,} OMS parent rows in {PARENT_TABLE}")

print("\n=== Source shape ===")
print(f"{PAYMENTS_SOURCE} rows: {spark.table(PAYMENTS_SOURCE).count():,}")
print(f"{ORDERS_SOURCE}  rows: {spark.table(ORDERS_SOURCE).count():,}")

print("\n=== payment_type distribution in bronze ===")
display(
    spark.table(PAYMENTS_SOURCE)
    .groupBy("payment_type").count()
    .orderBy(col("count").desc())
)

print("\n=== payment_installments distribution in bronze (top 15) ===")
display(
    spark.table(PAYMENTS_SOURCE)
    .groupBy("payment_installments").count()
    .orderBy(col("count").desc())
    .limit(15)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Target schema — reused from existing table

# COMMAND ----------

assert spark.catalog.tableExists(TARGET_TABLE), (
    f"{TARGET_TABLE} does not exist. Run 04_sa_tran_tender/sa_tran_tender_pos.py first."
)

target_schema  = spark.table(TARGET_TABLE).schema
TARGET_COLUMNS = [f.name for f in target_schema.fields]
print(f"Target schema has {len(TARGET_COLUMNS)} columns.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — join payments + orders for the parent timestamp
# MAGIC
# MAGIC Inner join: payments orphaned from orders couldn't compute a valid TRAN_SEQ_NO
# MAGIC anyway. Drop them now so they don't pollute the explode downstream.

# COMMAND ----------

orders_keyed = (
    spark.table(ORDERS_SOURCE)
    .select(
        col("order_id"),
        col("order_purchase_timestamp").cast(TimestampType()).alias("_parent_ts"),
    )
)

payments_with_parent = (
    spark.table(PAYMENTS_SOURCE)
    .join(orders_keyed, "order_id", "inner")
)

print(f"Bronze payment rows joined to parent orders: {payments_with_parent.count():,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — explode installments (1:N fan-out)
# MAGIC
# MAGIC `sequence(1, max(1, payment_installments))` generates `[1, 2, ..., N]`. The
# MAGIC `greatest` clamp protects against rare `payment_installments=0` rows (mostly
# MAGIC paired with `payment_type='not_defined'`) which would otherwise produce empty
# MAGIC arrays and silently drop the row.
# MAGIC
# MAGIC After explode, `TENDER_AMT = payment_value / installments_clamped`. Cast to
# MAGIC `DECIMAL(20, 4)` for precision; minor rounding (≤ 0.01 BRL per order on odd
# MAGIC divisions like 100/3) is expected and acceptable.

# COMMAND ----------

exploded = (
    payments_with_parent
    .withColumn(
        "_installments_clamped",
        greatest(col("payment_installments"), lit(1)).cast(IntegerType()),
    )
    .withColumn("_installment_idx", explode(sequence(lit(1), col("_installments_clamped"))))
    .withColumn(
        "_tender_amt_split",
        (col("payment_value") / col("_installments_clamped")).cast(DecimalType(20, 4)),
    )
)

print(f"After installment explode: {exploded.count():,} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — renumber TENDER_SEQ_NO per order
# MAGIC
# MAGIC Window function over `(payment_sequential ASC, installment_index ASC)` per
# MAGIC `order_id` gives contiguous 1..N TENDER_SEQ_NO values. Sort key intent:
# MAGIC payment_sequential is the bronze-source ordering (1st payment vs 2nd payment),
# MAGIC installment_index disambiguates installment fan-out within each payment.

# COMMAND ----------

w = Window.partitionBy("order_id").orderBy(
    col("payment_sequential").asc(),
    col("_installment_idx").asc(),
)

keyed_per_order = (
    exploded
    .withColumn("TENDER_SEQ_NO", row_number().over(w).cast(IntegerType()))
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — conform to silver shape

# COMMAND ----------

# TENDER_TYPE_GROUP mapping expressed as a chain of when() so we can pass through
# 'not_defined' → 'UNDEFINED' rather than rejecting it.
def map_tender_type_group():
    expr = lit(None).cast(StringType())
    for bronze_val, silver_val in TENDER_TYPE_MAP.items():
        expr = when(col("payment_type") == bronze_val, lit(silver_val)).otherwise(expr)
    return expr

flat = (
    keyed_per_order
    .select(
        # Parent surrogate inputs — IDENTICAL to sa_tran_head_olist.py + sa_tran_item_olist.py
        lit("OMS").alias("RTLOG_ORIG_SYS"),
        lit(OLIST_STORE).cast(LongType()).alias("STORE"),
        col("order_id").alias("TRAN_SEQ_NO_NATURAL"),
        col("_parent_ts").alias("TRAN_DATETIME"),

        # Tender PK component (already computed)
        col("TENDER_SEQ_NO"),

        # Tender attributes
        map_tender_type_group().alias("TENDER_TYPE_GROUP"),
        lit(None).cast(IntegerType()).alias("TENDER_TYPE_ID"),     # Olist has no equivalent
        col("_tender_amt_split").alias("TENDER_AMT"),

        # Diagnostics-only (kept in REF_NOs?  No — Olist tender notebook keeps these NULL.
        # The bronze payment_sequential and installment_idx are encoded in TENDER_SEQ_NO ordering.)
        lit(None).cast(StringType()).alias("CC_NO"),
        lit(None).cast(StringType()).alias("CC_AUTH_NO"),
        lit(None).cast(StringType()).alias("CC_ENTRY_MODE"),
        lit(None).cast(StringType()).alias("VOUCHER_NO"),

        # No foreign tender in Olist — keep both NULL
        lit(None).cast(StringType()).alias("ORIG_CURRENCY"),
        lit(None).cast(DecimalType(20, 4)).alias("ORIG_CURR_AMT"),

        # No error flags in Olist
        lit(None).cast(StringType()).alias("ERROR_IND"),
    )
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — surrogate + parent FK + BUSINESS_DATE inherit + USD + lineage

# COMMAND ----------

# 5a. Surrogate key — same hash as parent
keyed = flat.withColumn("TRAN_SEQ_NO", tran_seq_no_expr())

# 5b. FK enrich — inherits CURRENCY_CODE + FX_RATE from sa_tran_head
enriched = enrich_with_parent_fx(keyed, PARENT_TABLE, ["TRAN_SEQ_NO"])

# 5c. BUSINESS_DATE — also pulled from parent (partition column)
parent_bd = (
    spark.table(PARENT_TABLE)
    .select(
        col("TRAN_SEQ_NO").alias("_pbd_TRAN_SEQ_NO"),
        col("BUSINESS_DATE").alias("_pbd_BUSINESS_DATE"),
    )
)
enriched = (
    enriched
    .join(broadcast(parent_bd), col("TRAN_SEQ_NO") == col("_pbd_TRAN_SEQ_NO"), "left")
    .withColumn("BUSINESS_DATE", col("_pbd_BUSINESS_DATE"))
    .drop("_pbd_TRAN_SEQ_NO", "_pbd_BUSINESS_DATE")
)

# 5d. Derive USD companion + lineage
derived = (
    enriched
    .withColumn(
        "TENDER_AMT_USD",
        (col("TENDER_AMT") * col("FX_RATE")).cast(DecimalType(20, 4)),
    )
    .withColumn("_silver_ts", current_timestamp())
    .withColumn("_source",    lit(SOURCE_TABLE))
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6 — DQ split

# COMMAND ----------

dq = derived.withColumn(
    "rejection_reason",
    array_compact(array(
        when(~col("_has_parent"),
             lit("orphan_no_parent_header (TRAN_SEQ_NO not in sa_tran_head — order quarantined upstream)")),
        when(col("TENDER_SEQ_NO").isNull(),
             lit("TENDER_SEQ_NO null — row_number assignment failed")),
        when(col("TENDER_TYPE_GROUP").isNull(),
             lit("TENDER_TYPE_GROUP null — payment_type not in known map (CREDIT/DEBIT/VOUCHER/BOLETO/UNDEFINED)")),
        when(col("TENDER_AMT").isNull(),
             lit("TENDER_AMT null — payment_value missing or installment split failed")),
    )),
)

clean   = dq.filter("size(rejection_reason) = 0").drop("rejection_reason", "_has_parent")
rejects = dq.filter("size(rejection_reason) > 0").drop("_has_parent")
# _quarantine_ts added by merge_and_quarantine

# Project clean to exact target schema column order
clean = clean.select(*TARGET_COLUMNS)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7 — MERGE clean + append rejects

# COMMAND ----------

clean_n, reject_n = merge_and_quarantine(
    clean_df=clean,
    rejects_df=rejects,
    target_table=TARGET_TABLE,
    quarantine_table=QUARANTINE_TABLE,
    merge_keys=["TRAN_SEQ_NO", "TENDER_SEQ_NO"],
)
print(f"Olist Pass-3 tenders: clean={clean_n:,} rejects={reject_n:,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Validation (channel-filtered to OMS)

# COMMAND ----------

oms = spark.table(TARGET_TABLE).where(col("RTLOG_ORIG_SYS") == "OMS")
oms_count = oms.count()
total_count = spark.table(TARGET_TABLE).count()
print(f"silver.sa_tran_tender OMS row count:    {oms_count:,}")
print(f"silver.sa_tran_tender total row count:  {total_count:,}")

if spark.catalog.tableExists(QUARANTINE_TABLE):
    q_count = spark.table(QUARANTINE_TABLE).count()
    print(f"Olist tender quarantine row count:      {q_count:,}")
    if q_count > 0:
        print("\nTop Olist tender rejection reasons:")
        (
            spark.table(QUARANTINE_TABLE)
            .selectExpr("explode(rejection_reason) as reason")
            .groupBy("reason").count()
            .orderBy(col("count").desc())
            .show(truncate=False)
        )

if oms_count > 0:
    # 1. PK uniqueness within OMS
    dup_count = oms.groupBy("TRAN_SEQ_NO", "TENDER_SEQ_NO").count().where("count > 1").count()
    assert dup_count == 0, f"PK violation: {dup_count} duplicate (TRAN_SEQ_NO, TENDER_SEQ_NO) within OMS"
    print("PK uniqueness check passed (within OMS)")

    # 2. Cross-channel disjoint check
    pos_mkt_seqs = spark.table(TARGET_TABLE).where(col("RTLOG_ORIG_SYS") != "OMS").select("TRAN_SEQ_NO").distinct()
    oms_seqs     = oms.select("TRAN_SEQ_NO").distinct()
    overlap = oms_seqs.join(pos_mkt_seqs, "TRAN_SEQ_NO", "inner").count()
    assert overlap == 0, f"Cross-channel collision: {overlap} TRAN_SEQ_NO shared with POS/MKT"
    print("Cross-channel disjoint check passed (OMS vs POS/MKT)")

    # 3. TENDER_TYPE_GROUP NOT NULL (DQ rule already enforces, double-check post-write)
    bad_ttg = oms.where(col("TENDER_TYPE_GROUP").isNull()).count()
    assert bad_ttg == 0, f"{bad_ttg} OMS rows landed clean with NULL TENDER_TYPE_GROUP"
    print("TENDER_TYPE_GROUP NOT NULL check passed")

    # 4. TENDER_TYPE_GROUP distribution
    print("\nOMS TENDER_TYPE_GROUP distribution:")
    display(oms.groupBy("TENDER_TYPE_GROUP").count().orderBy(col("count").desc()))

    # 5. Installment fan-out diagnostic — orders with >1 tender row
    print("\nOMS tender cardinality distribution (rows per order):")
    display(
        oms.groupBy("TRAN_SEQ_NO").count()
        .groupBy("count").agg(f_sum(lit(1)).alias("orders_with_this_count"))
        .orderBy(col("count").asc())
        .limit(15)
    )

    # 6. Reconciliation diagnostic — head.VALUE vs SUM(TENDER_AMT) per order.
    #    NOT a hard assert: Olist payment-side may legitimately drift from item-side
    #    (item promotions / coupons applied at payment but not at item level).
    head_oms = spark.table(PARENT_TABLE).where(col("RTLOG_ORIG_SYS") == "OMS").select("TRAN_SEQ_NO", "VALUE")
    tender_totals = (
        oms.groupBy("TRAN_SEQ_NO")
        .agg(f_sum("TENDER_AMT").cast(DecimalType(20, 4)).alias("tender_roll_up"))
    )
    recon = (
        head_oms.alias("h")
        .join(tender_totals.alias("t"), "TRAN_SEQ_NO", "inner")
        .selectExpr(
            "TRAN_SEQ_NO",
            "CAST(h.VALUE AS DECIMAL(20,4)) AS head_value",
            "t.tender_roll_up",
            "ABS(h.VALUE - t.tender_roll_up) AS delta",
        )
    )
    n_recon = recon.count()
    n_drift_001    = recon.where(col("delta") > 0.01).count()
    n_drift_100bps = recon.where(col("delta") > col("head_value") * 0.01).count()
    print(f"\nReconciliation (head.VALUE vs SUM(TENDER_AMT)) — soft diagnostic, NOT a DQ assert:")
    print(f"  Orders reconciled:           {n_recon:,}")
    print(f"  Drift > 0.01 BRL:            {n_drift_001:,}")
    print(f"  Drift > 1% of head.VALUE:    {n_drift_100bps:,}")
    if n_drift_001 > 0:
        print("\n  Sample drifts (largest 10):")
        recon.where(col("delta") > 0.01).orderBy(col("delta").desc()).show(10, truncate=False)

    # 7. Sample — pick one order with multi-installment credit_card, show its tender lines
    print("\nSample multi-installment order — full tender line set:")
    sample_tran_row = (
        oms.groupBy("TRAN_SEQ_NO").count()
        .where(col("count") > 1)
        .limit(1)
        .collect()
    )
    if sample_tran_row:
        sample_tran = sample_tran_row[0].TRAN_SEQ_NO
        display(
            oms.where(col("TRAN_SEQ_NO") == sample_tran)
            .select("TRAN_SEQ_NO", "TENDER_SEQ_NO", "TENDER_TYPE_GROUP", "TENDER_AMT", "TENDER_AMT_USD")
            .orderBy("TENDER_SEQ_NO")
        )
    else:
        print("(no multi-row orders found — all Olist orders settled in a single tender row)")
