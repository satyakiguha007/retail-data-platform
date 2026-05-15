# Databricks notebook source

# COMMAND ----------
# MAGIC %md
# MAGIC # Bronze: Olist Brazilian E-commerce Dataset
# MAGIC
# MAGIC Ingests all 9 Olist CSV tables from the ADLS landing zone into
# MAGIC `bronze.olist_*` Delta tables.
# MAGIC
# MAGIC **Source dataset:** [Olist Brazilian E-commerce](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)
# MAGIC **Source path:** `{landing_root}/olist/`
# MAGIC **Target tables:** `bronze.olist_orders`, `bronze.olist_order_items`, etc.
# MAGIC **RTLOG_ORIG_SYS:** `'OMS'` (applied in Silver conformance, not Bronze)
# MAGIC **Schedule:** One-time historical load + daily incremental for new CSVs.
# MAGIC
# MAGIC All 9 tables are loaded idempotently — re-running overwrites the Bronze copy
# MAGIC (these are reference CSVs, not streaming sources).

# COMMAND ----------

dbutils.widgets.text("landing_root", "abfss://landing@<storage>.dfs.core.windows.net", "Landing zone root (local dev: use absolute path to data/landing)")
dbutils.widgets.text("catalog",      "retail",  "Unity Catalog name")
dbutils.widgets.text("bronze_schema","bronze",  "Bronze schema name")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.types import (
    DecimalType, DoubleType, IntegerType, StringType,
    StructField, StructType, TimestampType,
)

landing_root  = dbutils.widgets.get("landing_root").rstrip("/")
catalog       = dbutils.widgets.get("catalog")
bronze_schema = dbutils.widgets.get("bronze_schema")
olist_path    = f"{landing_root}/olist"

def tbl(name: str) -> str:
    return f"`{catalog}`.`{bronze_schema}`.{name}"

# COMMAND ----------
# MAGIC %md ## Table schemas

# COMMAND ----------

SCHEMAS: dict[str, StructType] = {

    "olist_orders": StructType([
        StructField("order_id",                        StringType(),    False),
        StructField("customer_id",                     StringType(),    False),
        StructField("order_status",                    StringType(),    False),
        StructField("order_purchase_timestamp",        StringType(),    True),
        StructField("order_approved_at",               StringType(),    True),
        StructField("order_delivered_carrier_date",    StringType(),    True),
        StructField("order_delivered_customer_date",   StringType(),    True),
        StructField("order_estimated_delivery_date",   StringType(),    True),
    ]),

    "olist_order_items": StructType([
        StructField("order_id",          StringType(),           False),
        StructField("order_item_id",     IntegerType(),          False),
        StructField("product_id",        StringType(),           False),
        StructField("seller_id",         StringType(),           False),
        StructField("shipping_limit_date", StringType(),         True),
        StructField("price",             DecimalType(18, 2),     False),
        StructField("freight_value",     DecimalType(18, 2),     False),
    ]),

    "olist_order_payments": StructType([
        StructField("order_id",            StringType(),         False),
        StructField("payment_sequential",  IntegerType(),        False),
        StructField("payment_type",        StringType(),         False),
        StructField("payment_installments",IntegerType(),        True),
        StructField("payment_value",       DecimalType(18, 2),   False),
    ]),

    "olist_order_reviews": StructType([
        StructField("review_id",                StringType(),    False),
        StructField("order_id",                 StringType(),    False),
        StructField("review_score",             IntegerType(),   True),
        StructField("review_comment_title",     StringType(),    True),
        StructField("review_comment_message",   StringType(),    True),
        StructField("review_creation_date",     StringType(),    True),
        StructField("review_answer_timestamp",  StringType(),    True),
    ]),

    "olist_customers": StructType([
        StructField("customer_id",              StringType(),    False),
        StructField("customer_unique_id",       StringType(),    False),
        StructField("customer_zip_code_prefix", StringType(),    True),
        StructField("customer_city",            StringType(),    True),
        StructField("customer_state",           StringType(),    True),
    ]),

    "olist_sellers": StructType([
        StructField("seller_id",                StringType(),    False),
        StructField("seller_zip_code_prefix",   StringType(),    True),
        StructField("seller_city",              StringType(),    True),
        StructField("seller_state",             StringType(),    True),
    ]),

    "olist_products": StructType([
        StructField("product_id",                     StringType(),   False),
        StructField("product_category_name",          StringType(),   True),
        StructField("product_name_lenght",            IntegerType(),  True),  # sic: original typo
        StructField("product_description_lenght",     IntegerType(),  True),  # sic
        StructField("product_photos_qty",             IntegerType(),  True),
        StructField("product_weight_g",               DoubleType(),   True),
        StructField("product_length_cm",              DoubleType(),   True),
        StructField("product_height_cm",              DoubleType(),   True),
        StructField("product_width_cm",               DoubleType(),   True),
    ]),

    "olist_product_category_name_translation": StructType([
        StructField("product_category_name",            StringType(), False),
        StructField("product_category_name_english",    StringType(), True),
    ]),

    "olist_geolocation": StructType([
        StructField("geolocation_zip_code_prefix", StringType(),    False),
        StructField("geolocation_lat",             DoubleType(),    True),
        StructField("geolocation_lng",             DoubleType(),    True),
        StructField("geolocation_city",            StringType(),    True),
        StructField("geolocation_state",           StringType(),    True),
    ]),
}

# COMMAND ----------
# MAGIC %md ## Ingest all tables

# COMMAND ----------

# Map our clean Delta table names → actual Kaggle CSV filenames.
# Kaggle uses _dataset suffix on most files; the translation table is the exception.
_KAGGLE_FILENAMES = {
    "olist_orders":                            "olist_orders_dataset.csv",
    "olist_order_items":                       "olist_order_items_dataset.csv",
    "olist_order_payments":                    "olist_order_payments_dataset.csv",
    "olist_order_reviews":                     "olist_order_reviews_dataset.csv",
    "olist_customers":                         "olist_customers_dataset.csv",
    "olist_sellers":                           "olist_sellers_dataset.csv",
    "olist_products":                          "olist_products_dataset.csv",
    "olist_product_category_name_translation": "product_category_name_translation.csv",
    "olist_geolocation":                       "olist_geolocation_dataset.csv",
}

from datetime import datetime
ingested_at = datetime.utcnow()

table_stats = []

for table_name, schema in SCHEMAS.items():
    csv_file = f"{olist_path}/{_KAGGLE_FILENAMES[table_name]}"
    target = tbl(table_name)

    # Read with enforced schema
    df = (
        spark.read
             .option("header", "true")
             .option("mode", "PERMISSIVE")   # log bad rows; don't fail the whole load
             .schema(schema)
             .csv(csv_file)
             .withColumn("_ingested_at",  F.lit(ingested_at).cast(TimestampType()))
             .withColumn("_source_file",  F.input_file_name())
    )

    row_count = df.count()

    # Overwrite: Olist CSVs are immutable reference files
    (
        df.write
          .format("delta")
          .mode("overwrite")
          .option("overwriteSchema", "true")
          .saveAsTable(target)
    )

    table_stats.append({"table": table_name, "rows": row_count})
    print(f"  {table_name:<50} {row_count:>8,} rows  ->  {target}")

# COMMAND ----------
# MAGIC %md ## Quality summary

# COMMAND ----------

print("\nQuality checks:")

# Orders: no duplicate order_ids
dup_orders = spark.sql(f"""
    SELECT COUNT(*) - COUNT(DISTINCT order_id) AS dup_count
    FROM {tbl('olist_orders')}
""").collect()[0]["dup_count"]
print(f"  Duplicate order_ids in olist_orders : {dup_orders}")

# Items: price > 0
zero_price = spark.sql(f"""
    SELECT COUNT(*) AS cnt
    FROM {tbl('olist_order_items')}
    WHERE price <= 0
""").collect()[0]["cnt"]
print(f"  Zero/negative price in order_items  : {zero_price}")

# Payments: payment_value > 0
zero_pay = spark.sql(f"""
    SELECT COUNT(*) AS cnt
    FROM {tbl('olist_order_payments')}
    WHERE payment_value <= 0
""").collect()[0]["cnt"]
print(f"  Zero/negative payment_value         : {zero_pay}")

# Orders referential integrity vs items
orphan_items = spark.sql(f"""
    SELECT COUNT(*) AS cnt
    FROM {tbl('olist_order_items')} i
    LEFT ANTI JOIN {tbl('olist_orders')} o ON i.order_id = o.order_id
""").collect()[0]["cnt"]
print(f"  Order items with no parent order    : {orphan_items}")

# COMMAND ----------
# MAGIC %md ## Load summary

# COMMAND ----------

summary_df = spark.createDataFrame(table_stats)
display(summary_df.orderBy("table"))
