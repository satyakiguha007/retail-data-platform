# ADLS Sync Console

Operator-grade UI for syncing local data sources to Azure Data Lake Storage Gen2.

Built for the **Retail Data Platform** to bridge local data generation
(POS simulator, marketplace feeds, reference batches) with cloud-side ingestion
(Auto Loader → Bronze → Silver → Gold).

---

## Features

- **Connection setup** — verifies `.env` configuration and tests the service principal against ADLS
- **Ad-hoc upload** — operator picks sources, sees validation alerts (missing stores/dates/marketplaces), runs an upload with live progress
- **Scheduler** — set up recurring background jobs (every N minutes/hours), pause/resume/delete on demand
- **Statistics & alerts** — local vs cloud file counts, upload history charts, active validation issues
- **Validation rules** built into each source: e.g., POS expects 3 specific stores on 2024-03-15 — alerts if any are missing

---

## Setup

### 1. Activate your project venv

From the **project root** (`retail-data-platform/`):

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
source .venv/bin/activate
```

### 2. Install dependencies

```bash
cd adls_sync_console
pip install -r requirements.txt
```

### 3. Verify `.env` exists at project root

The app reads from `<project_root>/.env` and expects these keys:

```
AZURE_TENANT_ID=...
AZURE_CLIENT_ID=...
AZURE_CLIENT_SECRET=...
AZURE_STORAGE_ACCOUNT=stretaildpsatyaki01
```

### 4. Run the app

From the `adls_sync_console/` directory:

```bash
streamlit run app.py
```

The app opens at <http://localhost:8501>.

---

## Folder layout

```
adls_sync_console/
├── app.py                      # Connection page (entry point)
├── pages/
│   ├── 01_Adhoc_Upload.py      # Pick sources, run upload now
│   ├── 02_Scheduler.py         # Add/manage recurring jobs
│   └── 03_Statistics.py        # History, charts, alerts
├── core/
│   ├── config.py               # Source definitions (path → container)
│   ├── adls.py                 # Azure SDK wrapper (credential, upload)
│   ├── sync.py                 # Sync engine (ThreadPoolExecutor)
│   ├── validate.py             # Expected vs found checks
│   ├── scheduler.py            # APScheduler background jobs
│   └── state.py                # JSON-based history persistence
├── data/
│   ├── jobs.json               # Scheduled jobs (auto-created)
│   └── history.json            # Run history (auto-created)
├── requirements.txt
└── README.md
```

---

## How it maps to your project

| Local path | ADLS target |
|---|---|
| `pos_simulator/sample_data/` | `raw/pos/` |
| `pos_simulator/output/smoke_test/` | `raw/pos/` |
| `pos_simulator/output/mkt_feed/` | `raw/marketplace/` |
| `ingestion/fx_rates/sample_data/` | `raw/fx-rates/` |
| `ingestion/weather/sample_data/` | `raw/weather/` |
| `data/landing/olist/` | `raw/olist/` |

Directory structure is preserved end-to-end. A file at
`pos_simulator/sample_data/store=1/date=2024-03-15/hour=06/rtlog.ndjson`
lands at
`abfss://raw@stretaildpsatyaki01.dfs.core.windows.net/pos/store=1/date=2024-03-15/hour=06/rtlog.ndjson`.

---

## Validation rules (per source)

| Source | What gets validated |
|---|---|
| POS sample | Stores `{1, 2, 3}`, date `2024-03-15` — alerts on missing |
| POS smoke test | Stores `{1, 2}`, dates `{2024-01-01, 2024-01-02}` |
| Marketplace | 7 marketplace IDs × 3 dates — alerts on missing |
| FX rates | At least 1 CSV present |
| Weather | At least 1 CSV present |
| Olist | 9 named CSV files expected — alerts on missing |

Edit `core/config.py` to change validation rules or add new sources.

---

## Troubleshooting

**"Missing env vars"** — check `.env` exists at project root with the 4 required keys.

**"AuthorizationFailed" when testing connection** — the service principal's role assignment hasn't propagated yet, or the secret has expired. Re-run the role assignment from step 3f of the provisioning guide, or rotate the secret with:

```bash
az ad sp credential reset --id sp-retaildp-simulator-001 --years 1
```

**Scheduler stops when I close Streamlit** — expected. The scheduler runs inside the Streamlit process. For 24/7 operation, deploy behind systemd / supervisord, or move scheduling to ADF / Airflow / Azure Functions.

**Files appear in wrong location in ADLS** — check the `remote_prefix` for that source in `core/config.py`.

---

## Architecture notes

- **Authentication**: `ClientSecretCredential` from `.env` — same pattern as `scripts/test_adls_connection.py`
- **Upload concurrency**: ThreadPoolExecutor with 8 workers per source — adjust `max_workers` in `sync.py` if your bandwidth is poor
- **Scheduling**: APScheduler `BackgroundScheduler` with `IntervalTrigger`. Jobs persist in JSON, reload on restart.
- **History**: capped at 500 most recent runs (configurable in `core/state.py`)
- **State**: pure JSON files in `data/` — no database dependency
