# Current project state

Last updated: 2026-05-06

## Done
- Repo scaffolded, CLAUDE.md with full conventions

- **Module 1 COMPLETE: POS RTLOG Simulator** (`pos_simulator/`)
  - 26 stores across India/USA/UK/UAE/Singapore (stores.csv)
  - All 8 TRAN_TYPEs, 6 fault types, IGTAX/TAX/BOTH modes
  - NDJSON partitioned store=/date=/hour= (Auto Loader ready)
  - CLI, Dockerfile, 41 passing tests, full docs

- **Module 2 COMPLETE: Batch Sources & Additional Channels** (`ingestion/`)
  - **FX rates** (`ingestion/fx_rates/`):
    - `generate_fx_rates.py` — synthetic GBM random walk, 5 currencies, 2023-2024
    - `fetch_fx_rates.py` — real ECB rates via Frankfurter API (free, no key)
    - `run_fx_rates.py` — interactive orchestrator: popup → fetch or generate → CSV
    - `bronze_fx_rates.py` — Databricks notebook, MERGE on (date × currency)
    - `sample_data/fx_rates_2023_2024.csv` — 3,655 rows fixture
  - **Marketplace simulator** (`ingestion/marketplace/`):
    - Full package: config, models, reference_data, generator, writer, main
    - 7 marketplaces (AMAZON_IN, FLIPKART_IN, MYNTRA_IN, AMAZON_US, AMAZON_UK, AMAZON_AE, LAZADA_SG)
    - 20 SKUs, DELIVERED/RETURNED/CANCELLED_REFUND, Singles Day + Black Friday spikes
    - NDJSON partitioned marketplace=/date=/ — rtlog_orig_sys: "MKT"
  - **Olist Bronze** (`ingestion/olist/`):
    - `bronze_olist.py` — all 9 tables, enforced StructType schemas, quality checks
  - **Weather** (`ingestion/weather/`):
    - `generate_weather.py` — synthetic seasonal climate profiles, 26 stores
    - `fetch_weather.py` — real ERA5 data via Open-Meteo API (free, no key)
    - `run_weather.py` — interactive orchestrator: popup → fetch or generate → CSV
    - `bronze_weather.py` — Databricks notebook, MERGE on (date × store)
    - `sample_data/weather_2023_2024.csv` — 19,006 rows fixture
  - **Shared UI** (`ingestion/ui_selector.py`):
    - Tkinter popups: ask_source() / ask_retry_or_fallback() / run_with_loading()
    - run_with_loading uses threading.Thread to keep UI alive during API calls
    - Both run_*.py scripts accept --mode synthetic/realtime to skip popup for automation

## In progress
- Nothing. Clean stopping point.

## Next task when returning
Start Module 3 — Medallion Lakehouse (`transformations/`).
Goal: Bronze → Silver ReSA conformance for all three channels:
  1. POS Auto Loader → `silver.sa_tran_*` (6 tables)
  2. Marketplace feed → `silver.sa_tran_*` (same schema, RTLOG_ORIG_SYS='MKT')
  3. Olist OMS orders → `silver.sa_tran_*` (RTLOG_ORIG_SYS='OMS')
  4. `silver.sa_store_day` — store-level daily summary
  5. FX normalisation using `bronze.fx_rates` for all monetary columns
  See design doc §4.3 and `docs/resa_reference.md` for column specs.
