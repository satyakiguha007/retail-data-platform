"""
Source configuration for the ADLS Sync Console.

Defines each data source: local path, remote target, file pattern,
and validation rules (expected stores/dates/marketplaces/files).
"""
from pathlib import Path

# Project root is one level up from this module's parent directory
# (adls_sync_console/core/config.py → adls_sync_console/ → project root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


SOURCES = [
    {
        "id": "pos_rtlog",
        "name": "POS RTLOGs",
        "icon": "store",
        "category": "POS",
        "local_root": "output/pos_rtlog",
        "remote_container": "raw",
        "remote_prefix": "pos",
        "file_pattern_glob": "store=*/date=*/hour=*/rtlog.ndjson",
        "validation": {
            "kind": "pos",
            # No strict expectations — UI will show whatever stores/dates are found.
            "stores": [],
            "dates": [],
        },
        "description": "Main POS RTLOG output from the simulator.",
    },
    {
        "id": "pos_smoke_test",
        "name": "POS RTLOGs — Smoke Test",
        "icon": "science",
        "category": "POS",
        "local_root": "output/smoke_test",
        "remote_container": "raw",
        "remote_prefix": "pos",
        "file_pattern_glob": "store=*/date=*/hour=*/rtlog.ndjson",
        "validation": {
            "kind": "pos",
            "stores": [1, 2],
            "dates": ["2024-01-01", "2024-01-02"],
        },
        "description": "2 stores × 2 days. Realistic-volume test run.",
    },
    {
        "id": "marketplace",
        "name": "Marketplace Feeds",
        "icon": "shopping_cart",
        "category": "Marketplace",
        "local_root": "output/mkt_feed",
        "remote_container": "raw",
        "remote_prefix": "marketplace",
        "file_pattern_glob": "marketplace=*/date=*/feed.ndjson",
        "validation": {
            "kind": "marketplace",
            "marketplaces": [
                "AMAZON_AE", "AMAZON_IN", "AMAZON_UK", "AMAZON_US",
                "FLIPKART_IN", "LAZADA_SG", "MYNTRA_IN",
            ],
            "dates": ["2024-03-15", "2024-03-16", "2024-03-17"],
        },
        "description": "7 marketplaces × 3 days.",
    },
    {
        "id": "fx_rates",
        "name": "FX Rates",
        "icon": "currency_exchange",
        "category": "Reference",
        "local_root": "ingestion/fx_rates/sample_data",
        "remote_container": "raw",
        "remote_prefix": "fx-rates",
        "file_pattern_glob": "*.csv",
        "validation": {
            "kind": "flat_csv",
            "min_files": 1,
        },
        "description": "Daily FX rates for 2023–2024.",
    },
    {
        "id": "weather",
        "name": "Weather Data",
        "icon": "cloud",
        "category": "Reference",
        "local_root": "ingestion/weather/sample_data",
        "remote_container": "raw",
        "remote_prefix": "weather",
        "file_pattern_glob": "*.csv",
        "validation": {
            "kind": "flat_csv",
            "min_files": 1,
        },
        "description": "Daily weather per store for 2023–2024.",
    },
    {
        "id": "olist",
        "name": "Olist E-commerce",
        "icon": "inventory_2",
        "category": "Reference",
        "local_root": "data/landing/olist",
        "remote_container": "raw",
        "remote_prefix": "olist",
        "file_pattern_glob": "*.csv",
        "validation": {
            "kind": "named_files",
            "expected_files": [
                "olist_customers_dataset.csv",
                "olist_orders_dataset.csv",
                "olist_order_items_dataset.csv",
                "olist_products_dataset.csv",
                "olist_sellers_dataset.csv",
                "olist_geolocation_dataset.csv",
                "olist_order_payments_dataset.csv",
                "olist_order_reviews_dataset.csv",
                "product_category_name_translation.csv",
            ],
        },
        "description": "Brazilian e-commerce dataset (Kaggle).",
    },
]


def get_source(source_id: str) -> dict | None:
    """Look up a source by ID."""
    for s in SOURCES:
        if s["id"] == source_id:
            return s
    return None


def resolve_local_root(source: dict) -> Path:
    """Get the absolute local path for a source."""
    return PROJECT_ROOT / source["local_root"]