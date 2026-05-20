# Retail Data Platform — Folder Structure

Lightweight reference for repo layout and ADLS cloud paths. Load as context when working on
ingestion, sync, or lakehouse modules.

---

## Local repository

```
retail-data-platform/
├── CLAUDE.md                          # Project conventions and module guide
├── .env                               # Azure credentials (gitignored)
├── pyproject.toml                     # Python project config / dependencies
│
├── pos_simulator/                     # Module 1 — POS RTLOG Simulator
│   ├── main.py                        # Entry point
│   ├── generator.py                   # Transaction generator
│   ├── models.py                      # Pydantic models
│   ├── writer.py                      # NDJSON writer
│   ├── faults.py                      # Fault injection
│   ├── config.py
│   ├── reference_data.py
│   ├── store_registry.py
│   ├── stores.csv
│   ├── Dockerfile
│   └── sample_data/                   # Static fixtures for tests
│       ├── store=1/date=2024-03-15/hour=*/rtlog.ndjson
│       ├── store=2/...
│       └── store=3/...
│
├── ingestion/                         # Module 2 — Batch Sources & Additional Channels
│   ├── olist/
│   │   └── bronze_olist.py            # Olist CSV → Bronze loader
│   ├── fx_rates/
│   │   ├── generate_fx_rates.py
│   │   ├── fetch_fx_rates.py
│   │   ├── bronze_fx_rates.py
│   │   ├── run_fx_rates.py
│   │   └── sample_data/               # Pre-generated CSVs
│   ├── weather/
│   │   ├── generate_weather.py
│   │   ├── fetch_weather.py
│   │   ├── bronze_weather.py
│   │   ├── run_weather.py
│   │   └── sample_data/               # Pre-generated CSVs
│   ├── marketplace/
│   │   ├── main.py
│   │   ├── generator.py
│   │   ├── models.py
│   │   ├── writer.py
│   │   ├── config.py
│   │   └── reference_data.py
│   └── ui_selector.py                 # Interactive source picker
│
├── adls_sync_console/                 # Module 2b — ADLS Sync Console
│   ├── app.py                         # Streamlit entry point (Connection page)
│   ├── pages/
│   │   ├── 01_Adhoc_Upload.py         # Pick sources, run upload now
│   │   ├── 02_Scheduler.py            # Add/manage recurring jobs
│   │   └── 03_Statistics.py           # History, charts, validation alerts
│   ├── core/
│   │   ├── config.py                  # Source definitions (local path → ADLS container)
│   │   ├── adls.py                    # Azure SDK wrapper (credential, upload)
│   │   ├── sync.py                    # Sync engine (ThreadPoolExecutor, 8 workers)
│   │   ├── validate.py                # Expected vs found checks per source
│   │   ├── scheduler.py               # APScheduler background jobs
│   │   └── state.py                   # JSON-based history persistence (500 runs cap)
│   ├── data/
│   │   ├── history.json               # Upload run history (auto-created)
│   │   └── jobs.json                  # Scheduled jobs (auto-created)
│   ├── requirements.txt
│   └── README.md
│
├── output/                            # Local simulator output (gitignored bulk data)
│   ├── pos_rtlog/
│   │   ├── store=1/date=*/hour=*/rtlog.ndjson
│   │   ├── store=2/...
│   │   └── store=3/...
│   ├── smoke_test/
│   │   ├── store=1/date=*/hour=*/rtlog.ndjson
│   │   └── store=2/...
│   └── mkt_feed/
│       ├── marketplace=AMAZON_AE/date=*/feed.ndjson
│       ├── marketplace=AMAZON_IN/...
│       ├── marketplace=AMAZON_UK/...
│       ├── marketplace=AMAZON_US/...
│       ├── marketplace=FLIPKART_IN/...
│       ├── marketplace=LAZADA_SG/...
│       └── marketplace=MYNTRA_IN/...
│
├── data/
│   └── landing/
│       └── olist/                     # 9 Kaggle CSVs (gitignored)
│           ├── olist_customers_dataset.csv
│           ├── olist_orders_dataset.csv
│           ├── olist_order_items_dataset.csv
│           ├── olist_products_dataset.csv
│           ├── olist_sellers_dataset.csv
│           ├── olist_geolocation_dataset.csv
│           ├── olist_order_payments_dataset.csv
│           ├── olist_order_reviews_dataset.csv
│           └── product_category_name_translation.csv
│
├── transformations/                   # Module 3 — Medallion Lakehouse (not started)
│   ├── bronze/
│   ├── silver/
│   └── gold/
│
├── audit/                             # Module 4 — Sales Audit Layer (not started)
├── llm/                               # Module 5 — LLM Intelligence Layer (not started)
│   ├── clients/
│   ├── review_enrichment/
│   ├── text_to_sql/
│   └── weekly_narrative/
├── serving/                           # Module 6 — Power BI (not started)
│
├── tests/
│   └── pos_simulator/
│       ├── conftest.py
│       ├── test_faults.py
│       ├── test_generator.py
│       └── test_writer.py
│
├── docs/
│   ├── folder_structure.md            # This file
│   ├── resa_reference.md              # Authoritative SA_* column definitions
│   ├── module1_pos_simulator.md
│   ├── module2_batch_sources_2026-05-06.md
│   ├── context_for_claude.md
│   └── retail_data_platform_design_v1.1.pdf
│
├── infra/
│   ├── modules/
│   └── environments/
│       ├── dev/
│       └── prod/
│
└── scripts/
    └── test_adls_connection.py        # Standalone ADLS connectivity test
```

---

## ADLS Gen2 — Cloud structure

**Storage account**: `stretaildpsatyaki01`  
**Auth**: Service principal `sp-retaildp-simulator-001` via `ClientSecretCredential`

```
stretaildpsatyaki01 (ADLS Gen2)
└── raw/                               # Landing container — Auto Loader watches this
    ├── pos/                           # POS RTLOGs (from simulator output + smoke test)
    │   ├── store=1/
    │   │   └── date=YYYY-MM-DD/
    │   │       └── hour=HH/
    │   │           └── rtlog.ndjson
    │   ├── store=2/...
    │   └── store=3/...
    │
    ├── marketplace/                   # Marketplace feeds (7 platforms)
    │   ├── marketplace=AMAZON_AE/
    │   │   └── date=YYYY-MM-DD/
    │   │       └── feed.ndjson
    │   ├── marketplace=AMAZON_IN/...
    │   ├── marketplace=AMAZON_UK/...
    │   ├── marketplace=AMAZON_US/...
    │   ├── marketplace=FLIPKART_IN/...
    │   ├── marketplace=LAZADA_SG/...
    │   └── marketplace=MYNTRA_IN/...
    │
    ├── olist/                         # Olist e-commerce CSVs (9 files, flat)
    │   ├── olist_customers_dataset.csv
    │   ├── olist_orders_dataset.csv
    │   ├── olist_order_items_dataset.csv
    │   ├── olist_products_dataset.csv
    │   ├── olist_sellers_dataset.csv
    │   ├── olist_geolocation_dataset.csv
    │   ├── olist_order_payments_dataset.csv
    │   ├── olist_order_reviews_dataset.csv
    │   └── product_category_name_translation.csv
    │
    ├── fx-rates/                      # Daily FX rate CSVs (flat)
    │   └── *.csv
    │
    └── weather/                       # Daily weather CSVs per store (flat)
        └── *.csv
```

### Key path convention
Directory structure is mirrored 1:1 from local to ADLS. A file at  
`output/pos_rtlog/store=1/date=2024-03-15/hour=06/rtlog.ndjson`  
lands at  
`abfss://raw@stretaildpsatyaki01.dfs.core.windows.net/pos/store=1/date=2024-03-15/hour=06/rtlog.ndjson`

Hive-style partitions (`store=`, `date=`, `hour=`, `marketplace=`) are preserved so Databricks
Auto Loader can infer them directly without schema hints.
