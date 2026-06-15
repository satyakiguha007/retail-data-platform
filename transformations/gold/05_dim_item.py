# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — `05_dim_item.py`
# MAGIC
# MAGIC Conformed item dimension. **SCD Type 2** — the only Type 2 dim in the model.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Target** | `retaildp.gold_core.dim_item` |
# MAGIC | **Grain** | One row per item *version* (a SKU can have multiple historical versions) |
# MAGIC | **Natural key** | `item` (SKU string) |
# MAGIC | **Surrogate key** | `item_key` (BIGINT, IDENTITY) — unique per version |
# MAGIC | **SCD** | **Type 2** on `dept, class, subclass, item_type, category, list_price` |
# MAGIC | **Sources** | `silver.sa_tran_item` (all 3 channels) + Olist product category translation |
# MAGIC
# MAGIC ## SCD2 tracked attributes (locked)
# MAGIC A change in ANY of these closes the current version and opens a new one:
# MAGIC `dept`, `class`, `subclass`, `item_type`, `category`, `list_price`.
# MAGIC
# MAGIC **Why `list_price`, not transaction price:** silver `UNIT_RETAIL` varies ±10% per sale,
# MAGIC so tracking it directly would create a version per transaction. Instead we derive a
# MAGIC stable per-SKU `list_price = MAX(unit_retail)` over non-return lines — reads as the
# MAGIC pre-discount catalog price, deterministic across re-runs, only changes on genuine
# MAGIC re-pricing. Actual per-sale price lives on `fact_sales_line.unit_retail`.
# MAGIC
# MAGIC ## SCD2 columns
# MAGIC - `effective_from TIMESTAMP` — when this version became current
# MAGIC - `effective_to   TIMESTAMP` — when it was superseded (NULL for current)
# MAGIC - `is_current     BOOLEAN`   — TRUE for exactly one row per SKU
# MAGIC - `version        INT`       — 1, 2, 3 ...
# MAGIC
# MAGIC ## Channel handling
# MAGIC - **POS / MKT** — `dept/class/subclass` are INT merch hierarchy; `category` derived from dept
# MAGIC - **Olist (OMS)** — `dept/class/subclass` NULL; `category` = Olist product_category (English)
# MAGIC - **Freight synthetic item** (`101010101`) — `category = 'Freight/Shipping'`, type `NMR`
# MAGIC
# MAGIC ## Idempotency
# MAGIC First run: every SKU inserts as version 1. Re-run on unchanged silver: no changes.
# MAGIC Re-run after a silver attribute change: old version expired, new version inserted.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 0. ⚠ One-time cleanup of any stale table
# MAGIC
# MAGIC Drop once if a prior `dim_item` exists with a different schema. No-op afterwards.

# COMMAND ----------

spark.sql("DROP TABLE IF EXISTS retaildp.gold_core.dim_item")
print("Dropped stale dim_item (if any). Run cells 1 onward.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration

# COMMAND ----------

CATALOG       = "retaildp"
SCHEMA        = "gold_core"
TABLE         = "dim_item"
TABLE_FQN     = f"{CATALOG}.{SCHEMA}.{TABLE}"

ITEM_SOURCE   = f"{CATALOG}.silver.sa_tran_item"
# Olist category translation (Portuguese -> English). Lives in bronze.
OLIST_PRODUCTS    = f"{CATALOG}.bronze.olist_products"
OLIST_CATEGORY_XL = f"{CATALOG}.bronze.olist_product_category_translation"

FREIGHT_ITEM  = "101010101"   # synthetic Olist freight line

print(f"Target:        {TABLE_FQN}")
print(f"Item source:   {ITEM_SOURCE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Discovery — what do item rows actually look like per channel?

# COMMAND ----------

print("=== sa_tran_item schema ===")
display(spark.sql(f"DESCRIBE TABLE {ITEM_SOURCE}"))

# COMMAND ----------

print("=== Item-attribute shape by channel (are dept/class/subclass null for OMS?) ===")
display(spark.sql(f"""
    SELECT
        rtlog_orig_sys,
        COUNT(*)                                                AS line_count,
        COUNT(DISTINCT item)                                    AS distinct_items,
        SUM(CASE WHEN dept     IS NULL THEN 1 ELSE 0 END)       AS null_dept,
        SUM(CASE WHEN class    IS NULL THEN 1 ELSE 0 END)       AS null_class,
        SUM(CASE WHEN subclass IS NULL THEN 1 ELSE 0 END)       AS null_subclass,
        SUM(CASE WHEN item     IS NULL THEN 1 ELSE 0 END)       AS null_item
    FROM {ITEM_SOURCE}
    GROUP BY rtlog_orig_sys
    ORDER BY rtlog_orig_sys
"""))

# COMMAND ----------

print("=== Distinct dept/class/subclass for POS+MKT (sanity: are these ints?) ===")
display(spark.sql(f"""
    SELECT DISTINCT dept, class, subclass
    FROM {ITEM_SOURCE}
    WHERE rtlog_orig_sys IN ('POS', 'MKT')
    ORDER BY dept, class, subclass
"""))

# COMMAND ----------

print("=== Does the Olist category translation table exist? ===")
for t in [OLIST_PRODUCTS, OLIST_CATEGORY_XL]:
    try:
        n = spark.table(t).count()
        print(f"✅ {t}: {n:,} rows")
    except Exception as e:
        print(f"❌ {t}: {str(e).splitlines()[0][:120]}")

# COMMAND ----------

print(f"=== Freight synthetic item ({FREIGHT_ITEM}) present? ===")
display(spark.sql(f"""
    SELECT rtlog_orig_sys, item, item_type, COUNT(*) AS n
    FROM {ITEM_SOURCE}
    WHERE item = '{FREIGHT_ITEM}'
    GROUP BY rtlog_orig_sys, item, item_type
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Create `dim_item` (SCD Type 2)

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {TABLE_FQN} (
    item_key        BIGINT     GENERATED ALWAYS AS IDENTITY  COMMENT 'Surrogate key — UNIQUE PER VERSION (not per SKU)',
    item            STRING     NOT NULL                      COMMENT 'Natural key — SKU. Repeats across versions.',
    item_type       STRING                                   COMMENT 'ReSA item type (ITM/GCN/NMR/...)',
    dept            INT                                       COMMENT 'Merch dept (NULL for Olist)',
    class           INT                                       COMMENT 'Merch class (NULL for Olist)',
    subclass        INT                                       COMMENT 'Merch subclass (NULL for Olist)',
    category        STRING                                   COMMENT 'Category label: POS/MKT derived from dept; Olist=product_category; freight=Freight/Shipping',
    list_price      DECIMAL(20,4)                            COMMENT 'Derived stable price = MAX(unit_retail) over non-return lines. SCD2-tracked.',
    source_channel  STRING                                   COMMENT 'Channel where this SKU first/primarily appears (POS/MKT/OLIST)',

    -- SCD Type 2 control columns
    effective_from  TIMESTAMP  NOT NULL                      COMMENT 'When this version became current',
    effective_to    TIMESTAMP                                COMMENT 'When superseded; NULL = current',
    is_current      BOOLEAN    NOT NULL                      COMMENT 'TRUE for exactly one row per SKU',
    version         INT        NOT NULL                      COMMENT 'Version counter 1,2,3...',

    _ingest_ts      TIMESTAMP  NOT NULL  DEFAULT current_timestamp()
)
USING DELTA
TBLPROPERTIES (
    'delta.feature.allowColumnDefaults' = 'supported',
    'delta.autoOptimize.optimizeWrite'  = 'true',
    'delta.autoOptimize.autoCompact'    = 'true'
)
COMMENT 'Conformed item dimension. SCD Type 2 on dept/class/subclass/item_type/category/list_price. Natural key=item (SKU); item_key unique per version.'
""")

print(f"{TABLE_FQN} ready.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Build current-state staging — one desired row per SKU
# MAGIC
# MAGIC Collapse `sa_tran_item` to one row per SKU with the tracked attributes:
# MAGIC - `dept/class/subclass/item_type` — take the most frequent (mode) per SKU to be robust
# MAGIC   against fault-injected outliers. Here we use `MAX` of the first-seen — simpler and
# MAGIC   deterministic since a SKU's hierarchy is stable in this dataset.
# MAGIC - `list_price` = `MAX(unit_retail)` over non-return lines (qty > 0)
# MAGIC - `category` — POS/MKT: derived from dept via a lookup; Olist: product_category (English)

# COMMAND ----------

from pyspark.sql import functions as F

# --- Olist product_id -> English category map ---
# olist_products.product_category_name (Portuguese) joined to the translation table.
try:
    olist_cat = (
        spark.table(OLIST_PRODUCTS).alias("p")
        .join(
            spark.table(OLIST_CATEGORY_XL).alias("x"),
            F.col("p.product_category_name") == F.col("x.product_category_name"),
            "left",
        )
        .select(
            F.col("p.product_id").alias("olist_item"),
            F.coalesce(
                F.col("x.product_category_name_english"),
                F.col("p.product_category_name"),
                F.lit("uncategorized"),
            ).alias("olist_category"),
        )
    )
    has_olist_cat = True
except Exception as e:
    print(f"⚠ Olist category join unavailable ({str(e).splitlines()[0][:80]}). "
          f"Olist categories will be NULL.")
    has_olist_cat = False

# --- dept -> category label for POS/MKT (derived; dept is INT in silver) ---
# Mapping mirrors the simulator's category assignment by dept band.
dept_category = spark.createDataFrame(
    [
        (10, "Electronics"),
        (20, "Fashion"),
        (30, "Home"),
        (40, "Books"),
        (50, "Sports"),
        (60, "Beauty"),
    ],
    ["dept_lk", "dept_category"],
)

# --- One desired row per SKU ---
base = spark.sql(f"""
    SELECT
        item,
        -- stable hierarchy per SKU (deterministic: min over the SKU's lines)
        MIN(dept)      AS dept,
        MIN(class)     AS class,
        MIN(subclass)  AS subclass,
        MIN(item_type) AS item_type,
        -- channel where this SKU primarily appears (min is deterministic)
        MIN(rtlog_orig_sys) AS source_channel,
        -- list price: max non-return unit_retail
        MAX(CASE WHEN qty > 0 THEN unit_retail END) AS list_price
    FROM {ITEM_SOURCE}
    WHERE item IS NOT NULL
    GROUP BY item
""")

# attach POS/MKT category from dept band
staged = (
    base
    .join(dept_category, base["dept"] == dept_category["dept_lk"], "left")
    .drop("dept_lk")
)

# attach Olist category by product_id
if has_olist_cat:
    staged = staged.join(olist_cat, staged["item"] == olist_cat["olist_item"], "left").drop("olist_item")
else:
    staged = staged.withColumn("olist_category", F.lit(None).cast("string"))

# resolve final category: freight > dept band (POS/MKT) > olist category > 'uncategorized'
staged = staged.withColumn(
    "category",
    F.when(F.col("item") == FREIGHT_ITEM, F.lit("Freight/Shipping"))
     .when(F.col("dept_category").isNotNull(), F.col("dept_category"))
     .when(F.col("olist_category").isNotNull(), F.col("olist_category"))
     .otherwise(F.lit("uncategorized")),
).drop("dept_category", "olist_category")

staged = staged.select(
    "item", "item_type", "dept", "class", "subclass",
    "category", "list_price", "source_channel",
)

staged.createOrReplaceTempView("dim_item_desired")

print(f"Desired distinct SKUs: {staged.count():,}")
display(staged.orderBy("source_channel", "item").limit(30))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Category coverage check

# COMMAND ----------

display(spark.sql("""
    SELECT
        source_channel,
        category,
        COUNT(*) AS sku_count
    FROM dim_item_desired
    GROUP BY source_channel, category
    ORDER BY source_channel, sku_count DESC
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. SCD2 apply — expire changed versions, insert new ones
# MAGIC
# MAGIC Standard Kimball SCD2 two-step:
# MAGIC 1. **Expire** current rows whose tracked attributes differ from the desired state:
# MAGIC    set `effective_to = now`, `is_current = FALSE`.
# MAGIC 2. **Insert** a new current version for: (a) brand-new SKUs, and (b) SKUs just expired.
# MAGIC
# MAGIC The `version` of an inserted row = prior max version for that SKU + 1 (or 1 if new).
# MAGIC `MERGE` cannot insert-and-update the same key in one pass, so we do step 1 as a MERGE
# MAGIC (update only) and step 2 as an INSERT from a computed set.

# COMMAND ----------

from pyspark.sql import functions as F

# Tracked-attribute equality: a row is "changed" if ANY tracked attr differs (null-safe).
# We compute the set of SKUs whose CURRENT version differs from desired (or have no current row).

current = spark.sql(f"""
    SELECT item, item_type, dept, class, subclass, category, list_price, version
    FROM {TABLE_FQN}
    WHERE is_current = TRUE
""")
current.createOrReplaceTempView("dim_item_current")

# SKUs needing a new version: no current row, OR any tracked attr changed
to_version = spark.sql("""
    SELECT d.*
    FROM dim_item_desired d
    LEFT JOIN dim_item_current c ON d.item = c.item
    WHERE c.item IS NULL
       OR (d.item_type  <=> c.item_type)  IS FALSE
       OR (d.dept       <=> c.dept)       IS FALSE
       OR (d.class      <=> c.class)      IS FALSE
       OR (d.subclass   <=> c.subclass)   IS FALSE
       OR (d.category   <=> c.category)   IS FALSE
       OR (d.list_price <=> c.list_price) IS FALSE
""")
to_version.createOrReplaceTempView("dim_item_to_version")

changed_count = to_version.count()
print(f"SKUs needing a new version (new or changed): {changed_count:,}")

# COMMAND ----------

# Step 1 — expire current rows that are being superseded (changed only, not brand-new)
spark.sql(f"""
    MERGE INTO {TABLE_FQN} AS tgt
    USING (SELECT item FROM dim_item_to_version) AS src
    ON tgt.item = src.item AND tgt.is_current = TRUE
    WHEN MATCHED THEN UPDATE SET
        effective_to = current_timestamp(),
        is_current   = FALSE
""")
print("Step 1: expired superseded current rows.")

# COMMAND ----------

# Step 2 — insert new current versions. version = prior max for SKU + 1 (or 1 if new).
spark.sql(f"""
    INSERT INTO {TABLE_FQN}
        (item, item_type, dept, class, subclass, category, list_price, source_channel,
         effective_from, effective_to, is_current, version)
    SELECT
        v.item, v.item_type, v.dept, v.class, v.subclass, v.category, v.list_price, v.source_channel,
        current_timestamp()                       AS effective_from,
        CAST(NULL AS TIMESTAMP)                    AS effective_to,
        TRUE                                       AS is_current,
        COALESCE(mx.max_version, 0) + 1            AS version
    FROM dim_item_to_version v
    LEFT JOIN (
        SELECT item, MAX(version) AS max_version
        FROM {TABLE_FQN}
        GROUP BY item
    ) mx ON v.item = mx.item
""")
print("Step 2: inserted new current versions.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Validation

# COMMAND ----------

print("=== Row / version summary ===")
display(spark.sql(f"""
    SELECT
        COUNT(*)                                       AS total_rows,
        COUNT(DISTINCT item)                           AS distinct_skus,
        SUM(CASE WHEN is_current THEN 1 ELSE 0 END)    AS current_rows,
        MAX(version)                                   AS max_version
    FROM {TABLE_FQN}
"""))

# COMMAND ----------

print("=== INVARIANT: exactly one current row per SKU (violations should be 0) ===")
display(spark.sql(f"""
    SELECT item, COUNT(*) AS current_count
    FROM {TABLE_FQN}
    WHERE is_current = TRUE
    GROUP BY item
    HAVING COUNT(*) <> 1
"""))

# COMMAND ----------

print("=== By source channel ===")
display(spark.sql(f"""
    SELECT source_channel,
           COUNT(*)                                     AS rows,
           COUNT(DISTINCT item)                         AS skus,
           SUM(CASE WHEN is_current THEN 1 ELSE 0 END)  AS current
    FROM {TABLE_FQN}
    GROUP BY source_channel
    ORDER BY source_channel
"""))

# COMMAND ----------

print("=== Sample current rows ===")
display(spark.sql(f"""
    SELECT item_key, item, item_type, dept, class, subclass, category,
           list_price, source_channel, version, is_current, effective_from
    FROM {TABLE_FQN}
    WHERE is_current = TRUE
    ORDER BY source_channel, item
    LIMIT 30
"""))

# COMMAND ----------

print("=== Coverage: every silver item has a current dim_item row ===")
missing = spark.sql(f"""
    SELECT s.item, COUNT(*) AS silver_line_count
    FROM {ITEM_SOURCE} s
    LEFT JOIN (SELECT item FROM {TABLE_FQN} WHERE is_current) d ON s.item = d.item
    WHERE s.item IS NOT NULL AND d.item IS NULL
    GROUP BY s.item
""")
if missing.count() > 0:
    print("❌ Silver items with no current dim_item row:")
    display(missing)
else:
    print("✅ Every silver item has a current dim_item row.")

# COMMAND ----------

print(f"✅ dim_item complete.")
print(f"   Total rows:    {spark.table(TABLE_FQN).count():,}")
print(f"   Current rows:  {spark.sql(f'SELECT COUNT(*) c FROM {TABLE_FQN} WHERE is_current').collect()[0]['c']:,}")
