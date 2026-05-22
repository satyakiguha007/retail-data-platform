# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze: Olist E-commerce (9 tables)
# MAGIC
# MAGIC **Source:** Flat CSVs at `abfss://raw@.../olist/*.csv` (9 files)
# MAGIC **Target:** `retaildp.bronze.olist_*` — 9 separate managed Delta tables
# MAGIC **Pattern:** Loop over table configs → strict StructType → PERMISSIVE mode → OVERWRITE
# MAGIC
# MAGIC Olist is a static Kaggle dataset (Brazilian e-commerce orders 2016–2018). Re-running this
# MAGIC notebook fully replaces each table — no MERGE needed. PERMISSIVE mode is used because the
# MAGIC source has known quirks (e.g., the misspelled `product_name_lenght` column preserved as-is,
# MAGIC nullable delivery timestamps for cancelled orders).
# MAGIC
# MAGIC ## Design differences from `01_fx_rates.py` and `02_weather.py`
# MAGIC
# MAGIC | Aspect | FX / Weather | Olist |
# MAGIC |---|---|---|
# MAGIC | Tables | 1 | 9 |
# MAGIC | Read mode | FAILFAST | PERMISSIVE (quirks expected) |
# MAGIC | Write mode | MERGE INTO | OVERWRITE (static historical data) |
# MAGIC | Structure | Linear cells | Config list + loop |

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration

# COMMAND ----------

CATALOG = "retaildp"
SCHEMA  = "bronze"

RAW_BASE       = "abfss://raw@stretaildpsatyaki01.dfs.core.windows.net/"
RAW_OLIST_PATH = RAW_BASE + "olist/"

spark.sql(f"USE CATALOG {CATALOG}")
print(f"Target catalog.schema: {CATALOG}.{SCHEMA}")
print(f"Source path:           {RAW_OLIST_PATH}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Schemas for all 9 tables
# MAGIC
# MAGIC Each schema is strict on types but permissive on nullability (most fields nullable except
# MAGIC primary keys). Olist preserves source typos like `product_name_lenght`.

# COMMAND ----------

from pyspark.sql.types import (
    StructType, StructField,
    StringType, IntegerType, DoubleType, TimestampType, DecimalType,
)

CUSTOMERS_SCHEMA = StructType([
    StructField("customer_id",              StringType(),  nullable=False),
    StructField("customer_unique_id",       StringType(),  nullable=True),
    StructField("customer_zip_code_prefix", IntegerType(), nullable=True),
    StructField("customer_city",            StringType(),  nullable=True),
    StructField("customer_state",           StringType(),  nullable=True),
])

ORDERS_SCHEMA = StructType([
    StructField("order_id",                      StringType(),    nullable=False),
    StructField("customer_id",                   StringType(),    nullable=True),
    StructField("order_status",                  StringType(),    nullable=True),
    StructField("order_purchase_timestamp",      TimestampType(), nullable=True),
    StructField("order_approved_at",             TimestampType(), nullable=True),
    StructField("order_delivered_carrier_date",  TimestampType(), nullable=True),
    StructField("order_delivered_customer_date", TimestampType(), nullable=True),
    StructField("order_estimated_delivery_date", TimestampType(), nullable=True),
])

ORDER_ITEMS_SCHEMA = StructType([
    StructField("order_id",            StringType(),       nullable=False),
    StructField("order_item_id",       IntegerType(),      nullable=False),
    StructField("product_id",          StringType(),       nullable=True),
    StructField("seller_id",           StringType(),       nullable=True),
    StructField("shipping_limit_date", TimestampType(),    nullable=True),
    StructField("price",               DecimalType(18, 2), nullable=True),
    StructField("freight_value",       DecimalType(18, 2), nullable=True),
])

PRODUCTS_SCHEMA = StructType([
    StructField("product_id",                 StringType(),  nullable=False),
    StructField("product_category_name",      StringType(),  nullable=True),
    # NOTE: source typos preserved (lenght instead of length) to match Olist Kaggle dataset
    StructField("product_name_lenght",        IntegerType(), nullable=True),
    StructField("product_description_lenght", IntegerType(), nullable=True),
    StructField("product_photos_qty",         IntegerType(), nullable=True),
    StructField("product_weight_g",           IntegerType(), nullable=True),
    StructField("product_length_cm",          IntegerType(), nullable=True),
    StructField("product_height_cm",          IntegerType(), nullable=True),
    StructField("product_width_cm",           IntegerType(), nullable=True),
])

SELLERS_SCHEMA = StructType([
    StructField("seller_id",              StringType(),  nullable=False),
    StructField("seller_zip_code_prefix", IntegerType(), nullable=True),
    StructField("seller_city",            StringType(),  nullable=True),
    StructField("seller_state",           StringType(),  nullable=True),
])

GEOLOCATION_SCHEMA = StructType([
    StructField("geolocation_zip_code_prefix", IntegerType(), nullable=False),
    StructField("geolocation_lat",             DoubleType(),  nullable=True),
    StructField("geolocation_lng",             DoubleType(),  nullable=True),
    StructField("geolocation_city",            StringType(),  nullable=True),
    StructField("geolocation_state",           StringType(),  nullable=True),
])

ORDER_PAYMENTS_SCHEMA = StructType([
    StructField("order_id",             StringType(),       nullable=False),
    StructField("payment_sequential",   IntegerType(),      nullable=False),
    StructField("payment_type",         StringType(),       nullable=True),
    StructField("payment_installments", IntegerType(),      nullable=True),
    StructField("payment_value",        DecimalType(18, 2), nullable=True),
])

ORDER_REVIEWS_SCHEMA = StructType([
    StructField("review_id",               StringType(),    nullable=False),
    StructField("order_id",                StringType(),    nullable=True),
    StructField("review_score",            IntegerType(),   nullable=True),
    StructField("review_comment_title",    StringType(),    nullable=True),
    StructField("review_comment_message",  StringType(),    nullable=True),
    StructField("review_creation_date",    TimestampType(), nullable=True),
    StructField("review_answer_timestamp", TimestampType(), nullable=True),
])

CATEGORY_TRANSLATION_SCHEMA = StructType([
    StructField("product_category_name",         StringType(), nullable=False),
    StructField("product_category_name_english", StringType(), nullable=True),
])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Table configuration — one entry per table

# COMMAND ----------

TABLES = [
    {"name": "olist_customers",                    "file": "olist_customers_dataset.csv",           "schema": CUSTOMERS_SCHEMA,            "pk": "customer_id"},
    {"name": "olist_orders",                       "file": "olist_orders_dataset.csv",              "schema": ORDERS_SCHEMA,               "pk": "order_id"},
    {"name": "olist_order_items",                  "file": "olist_order_items_dataset.csv",         "schema": ORDER_ITEMS_SCHEMA,          "pk": None},
    {"name": "olist_products",                     "file": "olist_products_dataset.csv",            "schema": PRODUCTS_SCHEMA,             "pk": "product_id"},
    {"name": "olist_sellers",                      "file": "olist_sellers_dataset.csv",             "schema": SELLERS_SCHEMA,              "pk": "seller_id"},
    {"name": "olist_geolocation",                  "file": "olist_geolocation_dataset.csv",         "schema": GEOLOCATION_SCHEMA,          "pk": None},
    {"name": "olist_order_payments",               "file": "olist_order_payments_dataset.csv",      "schema": ORDER_PAYMENTS_SCHEMA,       "pk": None},
    {"name": "olist_order_reviews",                "file": "olist_order_reviews_dataset.csv",       "schema": ORDER_REVIEWS_SCHEMA,        "pk": "review_id"},
    {"name": "olist_product_category_translation", "file": "product_category_name_translation.csv", "schema": CATEGORY_TRANSLATION_SCHEMA, "pk": "product_category_name"},
]

print(f"Configured {len(TABLES)} Olist tables for ingestion.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Ingestion loop
# MAGIC
# MAGIC For each table:
# MAGIC 1. Read CSV with strict schema in PERMISSIVE mode
# MAGIC 2. Add ingestion metadata (`_ingest_ts`, `_source_file`)
# MAGIC 3. Write Delta in OVERWRITE mode (replace, don't merge)
# MAGIC 4. Log row count + duplicate count on PK if applicable
# MAGIC
# MAGIC `overwriteSchema=true` is included so future schema evolutions don't require manual `DROP TABLE`.

# COMMAND ----------

from pyspark.sql.functions import col, current_timestamp

ingestion_results = []

for cfg in TABLES:
    table_name   = cfg["name"]
    source_file  = cfg["file"]
    schema       = cfg["schema"]
    pk           = cfg["pk"]
    full_path    = RAW_OLIST_PATH + source_file
    target_table = f"{CATALOG}.{SCHEMA}.{table_name}"

    print(f"\n=== {table_name} ===")
    print(f"  Source: {source_file}")
    print(f"  Target: {target_table}")

    # 4.1 Read
    raw_df = (
        spark.read
            .option("header", "true")
            .option("mode", "PERMISSIVE")
            .schema(schema)
            .csv(full_path)
    )
    row_count = raw_df.count()
    print(f"  Read:   {row_count:,} rows")

    # 4.2 Light PK check (where applicable) — Bronze tolerates duplicates, Silver enforces uniqueness
    dup_count = None
    if pk is not None:
        dup_count = (
            raw_df.groupBy(pk).count()
                  .filter(col("count") > 1)
                  .count()
        )
        if dup_count > 0:
            print(f"  WARN:   {dup_count:,} duplicate {pk} value(s) — will be preserved in Bronze (resolved in Silver)")
        else:
            print(f"  PK:     {pk} unique ✓")

    # 4.3 Add ingestion metadata
    staged = (
        raw_df
            .withColumn("_ingest_ts",   current_timestamp())
            .withColumn("_source_file", col("_metadata.file_path"))
    )

    # 4.4 Write OVERWRITE
    (
        staged.write
              .format("delta")
              .mode("overwrite")
              .option("overwriteSchema", "true")
              .saveAsTable(target_table)
    )
    print(f"  Wrote:  {target_table}")

    ingestion_results.append({
        "table":     table_name,
        "rows":      row_count,
        "pk":        pk or "—",
        "duplicates_on_pk": dup_count if dup_count is not None else "—",
    })

print(f"\nAll {len(TABLES)} tables ingested.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Cross-table summary

# COMMAND ----------

import pandas as pd

# Cast mixed-type columns to string for Arrow compatibility on serverless
df = pd.DataFrame(ingestion_results)
df["pk"]               = df["pk"].astype(str)
df["duplicates_on_pk"] = df["duplicates_on_pk"].astype(str)
display(df)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Apply table-level metadata (auto-optimize, comments)
# MAGIC
# MAGIC One ALTER TABLE per table — sets Delta auto-optimize and a descriptive comment.
# MAGIC These properties persist across overwrites.

# COMMAND ----------

TABLE_COMMENTS = {
    "olist_customers":                    "Bronze Olist — customer master from Brazilian e-commerce dataset",
    "olist_orders":                       "Bronze Olist — order header with status and delivery timestamps",
    "olist_order_items":                  "Bronze Olist — order line items with price and freight",
    "olist_products":                     "Bronze Olist — product master with dimensions (source typos preserved)",
    "olist_sellers":                      "Bronze Olist — seller master",
    "olist_geolocation":                  "Bronze Olist — zip code to lat/lng lookup (non-unique)",
    "olist_order_payments":               "Bronze Olist — order payment methods and installments",
    "olist_order_reviews":                "Bronze Olist — customer reviews and star ratings",
    "olist_product_category_translation": "Bronze Olist — Portuguese to English category name lookup",
}

for cfg in TABLES:
    t = f"{CATALOG}.{SCHEMA}.{cfg['name']}"
    spark.sql(f"""
        ALTER TABLE {t} SET TBLPROPERTIES (
            'delta.autoOptimize.optimizeWrite' = 'true',
            'delta.autoOptimize.autoCompact'   = 'true'
        )
    """)
    spark.sql(f"COMMENT ON TABLE {t} IS '{TABLE_COMMENTS[cfg['name']]}'")

print("Auto-optimize + comments applied to all 9 tables.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Sanity summary — row counts from Delta

# COMMAND ----------

display(spark.sql(f"""
SELECT 'olist_customers'                    AS table_name, COUNT(*) AS row_count FROM {CATALOG}.{SCHEMA}.olist_customers
UNION ALL SELECT 'olist_orders',                       COUNT(*) FROM {CATALOG}.{SCHEMA}.olist_orders
UNION ALL SELECT 'olist_order_items',                  COUNT(*) FROM {CATALOG}.{SCHEMA}.olist_order_items
UNION ALL SELECT 'olist_products',                     COUNT(*) FROM {CATALOG}.{SCHEMA}.olist_products
UNION ALL SELECT 'olist_sellers',                      COUNT(*) FROM {CATALOG}.{SCHEMA}.olist_sellers
UNION ALL SELECT 'olist_geolocation',                  COUNT(*) FROM {CATALOG}.{SCHEMA}.olist_geolocation
UNION ALL SELECT 'olist_order_payments',               COUNT(*) FROM {CATALOG}.{SCHEMA}.olist_order_payments
UNION ALL SELECT 'olist_order_reviews',                COUNT(*) FROM {CATALOG}.{SCHEMA}.olist_order_reviews
UNION ALL SELECT 'olist_product_category_translation', COUNT(*) FROM {CATALOG}.{SCHEMA}.olist_product_category_translation
ORDER BY table_name
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Spot-check — order status distribution (largest "real" table)

# COMMAND ----------

display(spark.sql(f"""
SELECT
    order_status,
    COUNT(*) AS order_count,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct
FROM {CATALOG}.{SCHEMA}.olist_orders
GROUP BY order_status
ORDER BY order_count DESC
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 9. Confirm physical location of one table (representative check)

# COMMAND ----------

display(spark.sql(f"DESCRIBE EXTENDED {CATALOG}.{SCHEMA}.olist_orders"))
# Look for the `Location` row — should start with abfss://bronze@stretaildpsatyaki01...
