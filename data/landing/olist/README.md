# Olist Dataset — Landing Zone

Place the 9 Olist CSV files here after downloading from Kaggle.

## Download

1. Go to: https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce
2. Click **Download** (top right) — downloads `archive.zip` (~45 MB)
3. Extract the zip — you will get a folder with the 9 CSV files
4. Copy all 9 files into THIS folder (`data/landing/olist/`)

## Expected files (exact Kaggle names — do not rename)

```
data/landing/olist/
  olist_orders_dataset.csv
  olist_order_items_dataset.csv
  olist_order_payments_dataset.csv
  olist_order_reviews_dataset.csv
  olist_customers_dataset.csv
  olist_sellers_dataset.csv
  olist_products_dataset.csv
  product_category_name_translation.csv      ← note: no olist_ prefix, no _dataset suffix
  olist_geolocation_dataset.csv
```

## What each file contains

| File | Rows (approx) | Description |
|---|---|---|
| `olist_orders_dataset.csv` | 99,441 | One row per order — the master record |
| `olist_order_items_dataset.csv` | 112,650 | Items within each order (one row per item) |
| `olist_order_payments_dataset.csv` | 103,886 | Payment method(s) per order |
| `olist_order_reviews_dataset.csv` | 99,224 | Customer star ratings and comments |
| `olist_customers_dataset.csv` | 99,441 | Customer location details |
| `olist_sellers_dataset.csv` | 3,095 | Seller location details |
| `olist_products_dataset.csv` | 32,951 | Product attributes and category |
| `product_category_name_translation.csv` | 71 | Portuguese → English category names |
| `olist_geolocation_dataset.csv` | 1,000,163 | Zip code → lat/lng mapping |

## Next step

Once files are here, run the Databricks Bronze notebook:
  `ingestion/olist/bronze_olist.py`

Set the widget `landing_root` to the absolute path of `data/landing`
(or the ADLS path in production).
