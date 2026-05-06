# Module 2 — Batch Sources & Additional Channels
**Date completed:** 2026-05-06
**Folder:** `ingestion/`
**Audience:** Beginner Python developer

---

## What is this module and why does it exist?

Module 1 gave us a POS (point-of-sale) data simulator — it generates fake in-store transactions.
But a real retail data platform has **more than one source of sales data**:

| Channel | What it is | Where it lives |
|---|---|---|
| In-store POS | Cashier scans items, customer pays | `pos_simulator/` (Module 1) |
| Marketplace (online) | Amazon, Flipkart, Myntra etc. send a daily settled-orders feed | `ingestion/marketplace/` |
| E-commerce (Olist) | A real public dataset of Brazilian online orders (9 CSV files) | `ingestion/olist/` |
| FX rates | Daily currency exchange rates (INR/GBP/AED/SGD vs USD) | `ingestion/fx_rates/` |
| Weather | Daily weather per store city (for basket analysis) | `ingestion/weather/` |

All four pieces in Module 2 land data into the **Bronze layer** — the raw, unmodified copy of
whatever the source system sends. Bronze is the foundation of the medallion lakehouse architecture:
`Bronze (raw) → Silver (conformed) → Gold (analytics-ready)`.

---

## Complete folder structure

```
ingestion/
├── __init__.py                         ← makes ingestion a Python package
├── ui_selector.py                      ← shared popup dialogs (used by fx + weather)
│
├── fx_rates/
│   ├── __init__.py
│   ├── generate_fx_rates.py            ← synthetic GBM generator (always available)
│   ├── fetch_fx_rates.py               ← real ECB data via Frankfurter API
│   ├── run_fx_rates.py                 ← interactive entry point (popup → fetch or generate)
│   ├── bronze_fx_rates.py              ← Databricks ingestion notebook
│   └── sample_data/
│       └── fx_rates_2023_2024.csv      ← pre-generated fixture (3,655 rows)
│
├── marketplace/
│   ├── __init__.py
│   ├── config.py
│   ├── models.py
│   ├── reference_data.py
│   ├── generator.py
│   ├── writer.py
│   └── main.py
│
├── olist/
│   └── bronze_olist.py                 ← Databricks notebook (all 9 tables)
│
└── weather/
    ├── __init__.py
    ├── generate_weather.py             ← synthetic seasonal generator (always available)
    ├── fetch_weather.py                ← real ERA5 data via Open-Meteo API
    ├── run_weather.py                  ← interactive entry point (popup → fetch or generate)
    ├── bronze_weather.py               ← Databricks ingestion notebook
    └── sample_data/
        └── weather_2023_2024.csv       ← pre-generated fixture (19,006 rows)
```

---

## Piece 1 — FX Rates

### What problem does it solve?

Our stores operate in **five currencies**: INR (India), USD (USA), GBP (UK), AED (UAE), SGD
(Singapore). When we later want to compare revenue across countries in the Silver layer, we need to
convert every transaction amount to a single reporting currency (USD). To do that, we need to know
the exchange rate on each specific date — not just a fixed number.

### Output format (same regardless of source)

```
rate_date,from_currency,to_currency,rate
2023-01-01,INR,USD,0.012116
2023-01-01,GBP,USD,1.203755
2023-01-01,AED,USD,0.272292
2023-01-01,SGD,USD,0.747767
2023-01-01,USD,USD,1.0
```

How to read this: `rate = 0.012116` for INR means "1 Indian Rupee = 0.012116 US Dollars on
2023-01-01". So to convert ₹5,000 to USD: `5000 × 0.012116 = $60.58`.

---

### File: `generate_fx_rates.py` — synthetic data (always works, no internet needed)

Generates **synthetic (fake but realistic) daily exchange rates** using a mathematical model called
**Geometric Brownian Motion (GBM)** — the same model used in finance for stock prices.

The formula for each day is:
```
new_rate = old_rate × exp(drift + shock)
```

- `drift` = a tiny daily trend (e.g. INR slowly depreciates ~1.8% per year vs USD)
- `shock` = a random Gaussian (bell-curve) noise term, scaled by the currency's daily volatility
- `exp(...)` = the exponential function, which ensures the rate never goes negative

**Key Python concepts used:**
- `math.exp()` and `math.sqrt()` — standard maths functions
- `random.gauss(0, 1)` — draw a random number from a bell curve (mean=0, stddev=1)
- `random.Random(seed)` — reproducible random number generator (same seed = same output every run)
- `csv.DictWriter` — write rows as dictionaries to a CSV file
- `argparse` — parse command-line arguments (`--start`, `--end`, `--seed`)

**Validation — what the output should look like:**
- INR starts ~0.01212 (Jan 2023) and ends ~0.01147 (Dec 2024) — gentle depreciation ✓
- AED barely moves (0.2722–0.2723) — it's pegged to USD ✓
- USD is always exactly 1.0 ✓

---

### File: `fetch_fx_rates.py` — real ECB data from the internet

Fetches **real daily exchange rates** from the
[Frankfurter API](https://api.frankfurter.app) — an open API maintained by Moritz Hager that
serves data directly from the **European Central Bank (ECB)**. It is completely free, requires
no account or API key, and supports historical rates back to 1999.

**One HTTP request fetches the entire date range:**
```
GET https://api.frankfurter.app/2023-01-01..2024-12-31?from=USD&to=INR,GBP,AED,SGD
```

The response is JSON like this (simplified):
```json
{
  "base": "USD",
  "rates": {
    "2023-01-02": {"INR": 82.685, "GBP": 0.8333, "AED": 3.6725, "SGD": 1.3371},
    "2023-01-03": {"INR": 82.769, "GBP": 0.8381, "AED": 3.6725, "SGD": 1.3385}
  }
}
```

**Two important things the fetcher handles:**

1. **Rate inversion** — The API gives "1 USD = X local" (e.g. 1 USD = 82.685 INR).
   Our schema needs the opposite: "1 local = Y USD" (1 INR = 1/82.685 = 0.01209 USD).
   So the fetcher divides: `our_rate = 1.0 / api_rate`.

2. **Forward-filling weekend/holiday gaps** — The ECB only publishes rates on trading days
   (weekdays, excluding major holidays like New Year's Day). Our data needs a rate for every
   calendar day including weekends. The fetcher walks every calendar day from start to end.
   If a day has no API data (e.g. a Saturday), it reuses the most recent available rate.
   This is the standard practice for daily FX lookups — the Saturday rate is Friday's rate.

**Python concepts used:**
- `urllib.request.urlopen()` — make an HTTP GET request using only the standard library
  (no pip install needed). `requests` is more popular but this avoids an extra dependency.
- `json.loads()` — parse the JSON response into a Python dictionary
- `urllib.error.URLError` / `HTTPError` — exceptions raised for network/API failures

---

### File: `bronze_fx_rates.py` — Databricks ingestion notebook

This is a **Databricks notebook** — it runs inside a Databricks workspace (Azure's managed Spark
environment). Think of it as a Python script with special features:

1. **Widgets** — like function parameters. `dbutils.widgets.text(...)` creates a text box in the
   Databricks UI where you type in the landing zone path, catalog name, etc.
2. **Spark** — reads the CSV in parallel across a cluster (handles billions of rows)
3. **Delta tables** — a storage format that adds ACID transactions and versioning to Parquet files

**What the notebook does step by step:**

1. **Create the table** if it doesn't exist (`CREATE TABLE IF NOT EXISTS`) — safe to run multiple times
2. **Read the CSV** with a strict schema — wrong type = immediate failure, not silent garbage
3. **Quality checks** — five assertions before writing (no zero rates, no future dates, valid currencies)
4. **MERGE** — update existing rows, insert new ones. Avoids duplicates and preserves history:
   ```sql
   MERGE INTO bronze.fx_rates AS tgt USING staged AS src
   ON tgt.rate_date = src.rate_date AND tgt.from_currency = src.from_currency ...
   WHEN MATCHED THEN UPDATE SET ...
   WHEN NOT MATCHED THEN INSERT *
   ```
5. **Summary report** — min/max rates per currency for a human sanity-check

---

## Piece 2 — Marketplace Feed Simulator

### What problem does it solve?

We need a second sales channel that is structurally different from POS:
- POS: customer walks into a store, cashier scans items → RTLOG format
- Marketplace: customer orders on Amazon/Flipkart, retailer fulfils → JSON feed delivered daily

The marketplace feed is **settled orders** — orders that have been fully completed (delivered,
returned, or refund confirmed). The retailer receives one JSON file per marketplace per day.

### Package structure

```
ingestion/marketplace/
├── __init__.py          ← marks this folder as a Python package
├── config.py            ← MarketplaceConfig dataclass (all settings in one place)
├── models.py            ← MktOrderItem and MktOrder dataclasses
├── reference_data.py    ← static lists: marketplaces, SKUs, promotions
├── generator.py         ← MktFeedGenerator class that creates orders
├── writer.py            ← writes orders to NDJSON files
└── main.py              ← CLI (command-line interface)
```

This deliberately mirrors the `pos_simulator/` structure from Module 1. Once you understand one,
the other is easy to follow.

---

### File: `reference_data.py`

Contains all the static lists the generator needs:

**`MARKETPLACES`** — 7 marketplaces, each with:
- Which countries' stores it serves (e.g. AMAZON_IN serves India stores only)
- Local currency (INR for India, USD for USA, etc.)
- Commission rate (what % the marketplace takes from every sale)

```python
{"marketplace": "AMAZON_IN", "store_countries": ["India"], "currency": "INR", "commission_rate": 0.15}
```

**`SKU_POOL`** — 20 products across 5 categories (Electronics, Fashion, Home, Books, Sports,
Beauty) with base prices in the local currency. Electronics has the highest base price (₹55,000 for
a laptop), Books the lowest (₹499).

**`ORDER_STATUS_WEIGHTS`** — 88% DELIVERED, 10% RETURNED, 2% CANCELLED_REFUND. These are weights
for Python's `random.choices()` — weighted random selection.

**`SETTLEMENT_LAG_WEIGHTS`** — most orders settle 3 days after placement (40%), some take 2 days
(30%), a few take 5–6 days. The lag represents the time between the customer placing an order and
the retailer's system recording it as settled.

---

### File: `models.py`

Two dataclasses that represent the data:

**`MktOrderItem`** — one line in an order (one product):
```python
@dataclass
class MktOrderItem:
    line_no: int
    sku: str
    dept: str
    class_: str   # note: class is a Python keyword, so we use class_ and rename in to_dict()
    subclass: str
    qty: int
    unit_price: float
    discount_amt: float
    total_amt: float
```

**`MktOrder`** — the full order, including a list of `MktOrderItem` objects:
```python
@dataclass
class MktOrder:
    order_id: str
    marketplace: str
    store_no: int
    ...
    items: list[MktOrderItem]
    ...
    rtlog_orig_sys: str = "MKT"   # channel discriminator used in Silver
```

Both have a `.to_dict()` method that converts the object to a plain Python dictionary suitable for
`json.dumps()`. The `class_` field is renamed to `"class"` in the output because JSON doesn't have
Python's keyword restriction.

> **Why dataclasses?** A `@dataclass` is like a regular Python class but Python automatically
> generates `__init__`, `__repr__`, and `__eq__` for you based on the field annotations.

---

### File: `generator.py`

The `MktFeedGenerator` class is the brain of the simulator.

**`__init__`** — reads the store registry (from `stores.csv`), then builds a mapping of which
stores each marketplace serves:
```python
self._mkt_stores = {
    "AMAZON_IN": [33487, 39876, 41203, ...],   # Indian stores
    "AMAZON_US": [70001, 70002, 70003, ...],   # US stores
    ...
}
```

**`generate_day(settle_date)`** — For each marketplace, for each store it serves, generates N
orders scaled by a day-of-week multiplier. Friday and Saturday are busiest for online shopping.

Special events: November 11 (Singles Day) → **5× spike**. Black Friday → **3.5×**.

**`_build_order()`** — creates one `MktOrder`:
1. Picks a random settlement lag (2–6 days) to get the `order_date`
2. Picks a status (DELIVERED, RETURNED, etc.)
3. Builds 1–3 items using `_build_items()`
4. Calculates `subtotal`, `discount`, `total`, `commission` using `Decimal` arithmetic

> **Why `Decimal` not `float`?** Python `float` is binary. `0.1 + 0.2` gives
> `0.30000000000000004` — a tiny error that compounds across millions of rows and breaks
> financial reconciliation. `Decimal` does exact decimal arithmetic.

---

### File: `writer.py`

Writes orders to **NDJSON** (Newline-Delimited JSON) — one complete JSON object per line. Ideal
for big data tools because each line can be parsed independently.

Partition structure mirrors POS simulator (Hive-style):
```
output/mkt_feed/
  marketplace=AMAZON_IN/date=2023-01-03/feed.ndjson
  marketplace=FLIPKART_IN/date=2023-01-03/feed.ndjson
```

The `key=value` folder naming is **Hive partitioning** — Databricks Auto Loader reads these
and automatically adds `marketplace` and `date` as columns in the dataframe.

---

### File: `main.py`

CLI with two commands:
```bash
py -3 -m ingestion.marketplace.main generate   # full 2023-2024 simulation
py -3 -m ingestion.marketplace.main sample     # print 3 days to screen
```

**Sample output (one order):**
```json
{
  "rtlog_orig_sys": "MKT",
  "order_id": "AMAZON_IN-20240312-000001",
  "marketplace": "AMAZON_IN",
  "store_no": 33487,
  "order_date": "2024-03-12",
  "settle_date": "2024-03-15",
  "currency": "INR",
  "customer_id": "CUST-5614226",
  "status": "DELIVERED",
  "items": [{"line_no": 1, "sku": "FASH-003", "qty": 1, "unit_price": 4175.58, ...}],
  "total_amt": 4175.58,
  "commission_rate": 0.15,
  "commission_amt": 626.34
}
```

---

## Piece 3 — Olist Bronze Notebook

### What is the Olist dataset?

[Olist](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) is a **real public dataset**
of ~100,000 Brazilian e-commerce orders from 2016–2018. It has 9 CSV files:

```
olist_orders          ← one row per order (the head record)
  └── olist_order_items      ← one row per item in each order
  └── olist_order_payments   ← how the customer paid
  └── olist_order_reviews    ← star rating + written review
        └── olist_customers  ← who placed the order
        └── olist_sellers    ← who fulfilled the item
        └── olist_products   ← what was sold
              └── olist_product_category_name_translation  ← Portuguese→English categories
olist_geolocation     ← zip code → lat/lng mapping
```

### File: `bronze_olist.py`

One Databricks notebook loops over all 9 tables. Key design choices:

**Enforced schemas** — each table has a `StructType`. Wrong column type = immediate failure.
```python
StructField("price",         DecimalType(18, 2), False),  # cannot be null
StructField("freight_value", DecimalType(18, 2), False),
```

**PERMISSIVE mode** — Olist is real data with known quirks (two columns have a typo:
`product_name_lenght` instead of `product_name_length` — preserved to match the source).

**Overwrite, not MERGE** — Olist CSVs are static. Overwriting is correct here.

**Quality checks:** no duplicate order_ids, no zero prices, no orphan items (referential integrity).

---

## Piece 4 — Weather Data

### What problem does it solve?

Weather affects retail sales — umbrellas sell when it rains, ice cream when it's hot. By joining
weather to transactions in the Gold layer, analysts can build weather-adjusted forecasts.

### Output format (same regardless of source)

```
obs_date,store_no,city,country,temp_max_c,temp_min_c,precipitation_mm,condition
2023-07-01,44512,Bangalore,India,30.8,18.2,6.0,RAINY
2023-07-01,70002,Minneapolis,USA,28.3,17.1,0.0,SUNNY
```

---

### File: `generate_weather.py` — synthetic data (always works, no internet needed)

Assigns a **climate profile** to each of the 26 stores (based on their city) and generates daily
observations using two mathematical models:

**Sinusoidal temperature:**
Temperature follows a smooth sine wave between the January minimum and July maximum:
```python
angle = math.pi * (month - 1) / 6   # 0 at January, π at July
mean = jan_mean + (jul_mean - jan_mean) * (1 - cos(angle)) / 2
```

**Gamma distribution for rainfall:**
On any given day: first decide if it rains (exponential probability), then sample the amount
from a gamma distribution — a skewed curve where most days have a little rain but occasionally
there's a downpour. This matches real-world rainfall patterns.

**Total output:** 19,006 rows = 731 days × 26 stores

---

### File: `fetch_weather.py` — real ERA5 data from the internet

Fetches **real historical weather** from the
[Open-Meteo API](https://open-meteo.com) — a free, open-source weather service that provides
ERA5 climate reanalysis data (European Centre for Medium-Range Weather Forecasts). Completely
free, no account or API key required, historical data back to 1940.

**Strategy — deduplicate cities before fetching:**
26 stores are spread across ~20 unique cities. Some cities have multiple stores (e.g. two stores
in Delhi, two in Mumbai). The fetcher first groups stores by their `(latitude, longitude)` pair
and makes **one API call per unique city**, then copies the result to all stores in that city.
This reduces 26 potential calls down to ~20 actual calls.

**API call (one per city):**
```
GET https://archive-api.open-meteo.com/v1/archive
    ?latitude=12.9716&longitude=77.5946
    &start_date=2023-01-01&end_date=2024-12-31
    &daily=temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode
    &timezone=auto
```

**Response structure:**
```json
{
  "daily": {
    "time":                ["2023-01-01", "2023-01-02", ...],
    "temperature_2m_max":  [33.2, 32.8, ...],
    "temperature_2m_min":  [18.5, 19.1, ...],
    "precipitation_sum":   [0.0, 0.2, ...],
    "weathercode":         [0, 3, ...]
  }
}
```

**WMO weather code → CONDITION label:**

The API returns numeric WMO codes. The fetcher maps these to our human-readable labels:

| WMO code(s) | Meaning | Our label |
|---|---|---|
| 0, 1 | Clear sky / mainly clear | SUNNY |
| 2 | Partly cloudy | PARTLY_CLOUDY |
| 3, 45, 48 | Overcast / fog | CLOUDY |
| 51–67 | Drizzle / rain | RAINY |
| 71–77 | Snow / sleet | SNOWY |
| 80–82 | Rain showers | RAINY |
| 85, 86 | Snow showers | SNOWY |
| 95, 96, 99 | Thunderstorm | STORMY |

**Polite delay:** A 0.4-second pause between city requests avoids hammering the API.

---

### File: `bronze_weather.py` — Databricks ingestion notebook

Same MERGE pattern as FX rates:
- MERGE on `(obs_date, store_no)` for idempotency
- Temperature sanity: between -60°C and +60°C, max ≥ min
- Precipitation non-negative
- Condition must be one of the 6 valid codes

---

## Piece 5 — Interactive Data Source UI

### Why this exists

Both FX rates and weather have two data sources: a synthetic generator (always available, no
internet needed) and a real API fetcher (live/historical data, requires internet). Rather than
hard-coding which one to use, the user gets a **popup dialog** at runtime to choose.

The rule:
- For **historical date ranges** (end date in the past) → both options available
- For **today or recent dates** → real-time fetch gives accurate current data
- For **development/testing/offline** → synthetic generation always works

---

### File: `ingestion/ui_selector.py` — shared popup library

This file is shared by both `run_fx_rates.py` and `run_weather.py`. It contains three functions:

---

#### `ask_source(data_type, date_range)` → `"realtime"` or `"synthetic"`

Shows a blue/grey two-button popup:

```
┌─────────────────────────────────────────────────────┐
│  Data Source  —  FX Rates                           │
│  Date range: 2023-01-01 to 2024-12-31               │
│  ─────────────────────────────────────────────────  │
│  [ 🌐 Fetch Real-Time from API ]  [ ⚙️ Generate ]   │
└─────────────────────────────────────────────────────┘
```

Returns `"realtime"` or `"synthetic"`. If the user closes the window without clicking,
defaults to `"synthetic"` (safest fallback).

**How Tkinter dialogs work:**
```python
result = {"choice": "synthetic"}   # default stored in a dict (not a plain variable)
                                   # because a nested function can't reassign an outer variable
root = tk.Tk()
# ... add labels and buttons ...

def choose(val):
    result["choice"] = val
    root.destroy()   # <-- this exits mainloop() below

root.mainloop()   # blocks here until root.destroy() is called
return result["choice"]
```

> **Why a dict instead of a plain variable?**
> The `choose` function is a **closure** — it lives inside `ask_source` and captures the
> outer scope. In Python, you cannot reassign a simple variable from an inner function
> (`result = val` would create a new local variable). But you *can* mutate a mutable object
> like a dict (`result["choice"] = val`). This is a standard Tkinter pattern.

---

#### `ask_retry_or_fallback(error_msg)` → `"retry"` or `"fallback"`

Shown when the API fetch fails. Red-tinted window with the error message and two buttons:

```
┌─────────────────────────────────────────────────────┐
│  ⚠️  API Fetch Failed                               │
│  HTTPError: 503 Service Unavailable                 │
│  ─────────────────────────────────────────────────  │
│  [ 🔄 Try Again ]  [ ⚙️ Use Synthetic Data ]        │
└─────────────────────────────────────────────────────┘
```

---

#### `run_with_loading(task_fn, message)` → result (or raises exception)

Shows a "please wait" window while running a potentially slow function (like an API call) in a
**background thread**.

**Why a background thread?**
Tkinter requires its main thread to keep running its event loop so the window stays alive,
responsive, and painted on screen. If the network call ran on the main thread, the window would
freeze, appear unresponsive, and potentially show "Not Responding" on Windows.

The solution:
```
Main thread                          Background thread
────────────                         ─────────────────
show loading window
root.update()     →  window appears
start thread      →                  task_fn() runs (e.g. API call)
root.mainloop()   →  window alive    ...
  (blocking)                         ...done → root.after(0, root.destroy)
                  ←  destroy fires
mainloop() exits
re-raise exception if any
return result
```

The key detail: **`root.after(0, root.destroy)`** — you can never call Tkinter functions directly
from a non-main thread. `root.after(0, fn)` safely schedules `fn` to run on the main thread at
the next opportunity (effectively: immediately, but safely).

---

### Files: `run_fx_rates.py` and `run_weather.py` — the orchestrators

These are the scripts the user actually runs. They implement the same retry loop:

```python
source = ask_source("FX Rates", date_range)   # popup

if source == "synthetic":
    rows = generate_synthetic(start, end)

else:  # realtime — retry loop
    while rows is None:
        try:
            rows = run_with_loading(
                task_fn=lambda: fetch_realtime(start, end),
                message="Fetching from Frankfurter API..."
            )
        except Exception as exc:
            decision = ask_retry_or_fallback(str(exc))
            if decision == "retry":
                pass      # loop continues → tries API again
            else:
                rows = generate_synthetic(start, end)  # fallback

write_csv(rows, out_path)
```

**Both scripts accept a `--mode` flag to skip the popup entirely:**
```bash
py -3 ingestion/fx_rates/run_fx_rates.py --mode synthetic    # no popup
py -3 ingestion/fx_rates/run_fx_rates.py --mode realtime     # no popup, goes straight to API
```
This is useful for automated/scheduled runs where no human is present.

**`sys.path` injection — how standalone scripts import from a package:**
Both run scripts add the project root to `sys.path` at the top:
```python
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
```
This makes `from ingestion.ui_selector import ...` work when the script is run directly
(`py -3 ingestion/fx_rates/run_fx_rates.py`) rather than as a module
(`py -3 -m ingestion.fx_rates.run_fx_rates`). Without it, Python can't find the `ingestion`
package because the script's own folder (`fx_rates/`) is on the path, not the project root.

---

## How to run everything

### Interactive (popup appears)
```bash
# FX rates — user chooses real API or synthetic via popup
py -3 ingestion/fx_rates/run_fx_rates.py

# Weather — user chooses real API or synthetic via popup
py -3 ingestion/weather/run_weather.py

# Custom date range (e.g. for today's rates)
py -3 ingestion/fx_rates/run_fx_rates.py --start 2025-01-01 --end 2026-05-06
```

### Non-interactive (skip popup, useful for automation)
```bash
# Force synthetic (no internet needed)
py -3 ingestion/fx_rates/run_fx_rates.py --mode synthetic
py -3 ingestion/weather/run_weather.py --mode synthetic

# Force real-time (skips popup, fails with error if API is down)
py -3 ingestion/fx_rates/run_fx_rates.py --mode realtime
py -3 ingestion/weather/run_weather.py --mode realtime
```

### Direct generators (bypasses UI entirely)
```bash
# These are the original scripts, unchanged:
py -3 ingestion/fx_rates/generate_fx_rates.py
py -3 ingestion/weather/generate_weather.py
```

### Marketplace simulator
```bash
py -3 -m ingestion.marketplace.main generate   # full 2023-2024 run
py -3 -m ingestion.marketplace.main sample     # 3-day sample to screen
```

> **Note:** The Databricks notebooks (`bronze_*.py`) cannot be run locally — they use `spark`,
> `dbutils`, and `display()` which only exist inside a Databricks workspace.

---

## How everything connects

```
pos_simulator/
  stores.csv ──────────────────────── shared store registry
                                       (used by POS generator AND marketplace simulator)

ingestion/
  ui_selector.py ──────────────────── shared popup UI
                                       (used by run_fx_rates.py AND run_weather.py)

  fx_rates/
    fetch_fx_rates.py ─── ECB API ─┐
    generate_fx_rates.py ──────────┴→  CSV  →  Bronze  →  bronze.fx_rates
    run_fx_rates.py  (UI orchestrator)         (used in Silver to normalise to USD)

  marketplace/
    generator + writer ───────────────  NDJSON  →  Bronze  →  bronze.mkt_feed
                                                    RTLOG_ORIG_SYS='MKT'
                                                    (SA_TRAN_* in Silver)

  olist/
    bronze_olist.py ──────────────────  9 CSVs  →  Bronze  →  bronze.olist_*
                                                    RTLOG_ORIG_SYS='OMS'
                                                    (SA_TRAN_* in Silver)

  weather/
    fetch_weather.py ── Open-Meteo ─┐
    generate_weather.py ────────────┴→  CSV  →  Bronze  →  bronze.weather
    run_weather.py  (UI orchestrator)           (joined in Gold for demand analysis)
```

---

## Key Python and Data Engineering concepts in this module

| Concept | Where used | What it means |
|---|---|---|
| `@dataclass` | `marketplace/models.py` | Auto-generates `__init__` from field annotations |
| `random.gauss()` | `generate_fx_rates.py` | Draw from a bell-curve distribution |
| `math.exp()` | `generate_fx_rates.py` | Exponential — used in GBM formula |
| `Decimal` arithmetic | `marketplace/generator.py` | Exact decimal maths for financial values |
| `argparse` | all run scripts | Parse CLI flags like `--start 2023-01-01` |
| `csv.DictWriter` | all generators | Write CSV with headers from dict keys |
| Hive partitioning | `marketplace/writer.py` | `key=value` folder structure for big data |
| NDJSON | `marketplace/writer.py` | One JSON object per line — big data friendly |
| Databricks widgets | Bronze notebooks | UI parameters for notebooks |
| Delta MERGE | Bronze notebooks | Upsert — update if exists, insert if not |
| Enforced schemas | `bronze_olist.py` | `StructType` forces column types on read |
| Geometric Brownian Motion | `generate_fx_rates.py` | Financial model for random walks |
| Gamma distribution | `generate_weather.py` | Skewed distribution for rainfall amounts |
| Sinusoidal interpolation | `generate_weather.py` | Smooth seasonal temperature curve |
| `urllib.request` | `fetch_fx_rates.py`, `fetch_weather.py` | HTTP calls without pip install |
| `json.loads()` | fetch scripts | Parse JSON API responses into Python dicts |
| WMO weather codes | `fetch_weather.py` | Standard numeric codes mapped to labels |
| Tkinter | `ui_selector.py` | Python's built-in GUI library for popup dialogs |
| `threading.Thread` | `ui_selector.py` | Run API call in background, keep UI alive |
| `root.after(0, fn)` | `ui_selector.py` | Thread-safe way to call Tkinter from another thread |
| Closure + dict pattern | `ui_selector.py` | How to return a value from a button callback |
| `sys.path` injection | `run_fx_rates.py`, `run_weather.py` | Let a script import its own package |
| Forward-filling | `fetch_fx_rates.py` | Propagate last known value to fill weekend gaps |
