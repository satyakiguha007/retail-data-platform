# Module 1 — POS RTLOG Simulator

**Folder:** `pos_simulator/`
**Status:** Complete
**Built by:** Satyaki Guha

---

## Table of Contents

1. [What this module does](#1-what-this-module-does)
2. [Why it exists](#2-why-it-exists)
3. [Output format](#3-output-format)
4. [Code walkthrough — file by file](#4-code-walkthrough--file-by-file)
5. [Fault injection catalog](#5-fault-injection-catalog)
6. [Configuration reference](#6-configuration-reference)
7. [How to run](#7-how-to-run)
8. [Running with Docker](#8-running-with-docker)
9. [Tests](#9-tests)
10. [Design decisions](#10-design-decisions)
11. [What this feeds downstream](#11-what-this-feeds-downstream)

---

## 1. What this module does

The POS RTLOG Simulator is a Python package that generates realistic, Oracle ReSA-shaped retail
transaction data. It writes newline-delimited JSON (NDJSON) files to a local or mounted directory,
partitioned by `store / date / hour` — exactly the structure Azure Auto Loader expects.

**Key numbers (default config):**
- 50 stores × 4 tills each
- ~150 SALE transactions per till per day (volume knob — see §6)
- 2 years of history (2023-01-01 → 2024-12-31) = ~365 M transaction lines
- ~2 % of records are intentionally bad (fault injection — see §5)
- Every record carries `RTLOG_ORIG_SYS = 'POS'`

**Transaction types generated:**

| TRAN_TYPE | Mix   | Description |
|-----------|-------|-------------|
| SALE      | ~85 % | Normal retail purchase |
| RETURN    |  ~7 % | Customer return with reason code |
| PVOID     |  ~3 % | Post-void (supervisor correction) |
| PAIDIN    |  ~1 % | Cash paid in (vendor payment) |
| PAIDOUT   |  ~1 % | Cash paid out (petty cash) |
| NOSALE    |  ~2 % | No-sale (drawer open, no transaction) |
| OPEN      | 1/till/day | Till open at ~06:00 |
| CLOSE     | 1/till/day | Till close at ~22:00 |

---

## 2. Why it exists

Module 3 (Medallion Lakehouse) and Module 4 (Sales Audit) both need realistic, schema-correct POS
data from day one. Rather than using a generic CSV dataset, this simulator produces records whose
field names, types, and nullability match the Oracle RMS 16.0 data model exactly — the same model
the author has worked with professionally at Capgemini for 7+ years.

Any retail ETL engineer who sees the output immediately recognises it as valid RTLOG data. That
recognition is the portfolio differentiator.

---

## 3. Output format

### File layout

```
output/pos_rtlog/
  store=1/
    date=2024-01-01/
      hour=06/
        rtlog.ndjson      <- newline-delimited JSON, one record per line
      hour=07/
        rtlog.ndjson
      ...
  store=2/
    ...
```

The partition scheme (`store=` / `date=` / `hour=`) is the Hive-style format that Azure Auto Loader
reads with `cloudFiles.format = 'json'` and Databricks DLT picks up incrementally.

### Record shape

Each line in an NDJSON file is one complete transaction — the `tran_head` plus all its child arrays.
The Silver conformance job in Module 3 will fan this out into the six `SA_TRAN_*` Delta tables.

```json
{
  "rtlog_orig_sys": "POS",
  "tran_head": {
    "store": 45,
    "business_date": "2024-03-15",
    "tran_seq_no": "STR045-TILL02-2024-03-15-001234",
    "tran_datetime": "2024-03-15T12:34:22+05:30",
    "register": "TILL02",
    "tran_no": 1234,
    "cashier": "CASH1089",
    "salesperson": "SP0456",
    "tran_type": "SALE",
    "sub_tran_type": null,
    "status": "IMPORTED",
    "value": 594.8200,
    "pos_tran_ind": "Y",
    "error_ind": "N",
    "banner_no": 1,
    "rtlog_orig_sys": "POS",
    "update_datetime": "2024-03-15T12:34:22+05:30",
    "update_id": "SIM",
    "rev_no": 0,
    "ref_no1": "LOYALTY-9988123",
    "ref_no2": null
  },
  "tran_item": [
    {
      "item_seq_no": 1,
      "item_status": "ACTIVE",
      "item_type": "ITM",
      "item": "APP-M-0002",
      "dept": 10,
      "class": 1,
      "subclass": 1,
      "qty": 2.0,
      "unit_retail": 999.0000,
      "selling_uom": "EA",
      "tax_ind": "Y",
      "item_swiped_ind": "N",
      "error_ind": "N",
      "drop_ship_ind": "N",
      "unit_retail_vat_incl": "Y",
      "total_igtax_amt": 359.6400,
      "uom_quantity": 2.0,
      "return_reason_code": null,
      "override_reason": null,
      "orig_unit_retail": null,
      "cust_order_no": null
    }
  ],
  "tran_disc": [
    {
      "item_seq_no": 1,
      "discount_seq_no": 1,
      "rms_promo_type": "PROMO",
      "promotion": 500002,
      "disc_type": "PROMO",
      "qty": 2.0,
      "unit_discount_amt": 149.8500,
      "error_ind": "N",
      "uom_quantity": 2.0,
      "coupon_no": null,
      "promo_comp": null
    }
  ],
  "tran_tender": [
    {
      "tender_seq_no": 1,
      "tender_type_group": "CARD",
      "tender_type_id": 2,
      "tender_amt": 594.8200,
      "error_ind": "N",
      "cc_no": "************4521",
      "cc_auth_no": "A12345",
      "cc_entry_mode": "CHIP",
      "orig_currency": "INR",
      "orig_curr_amt": 594.8200,
      "voucher_no": null
    }
  ],
  "tran_tax": [],
  "tran_igtax": [
    {
      "item_seq_no": 1,
      "igtax_seq_no": 1,
      "tax_authority": "CGST",
      "igtax_code": "GST9",
      "igtax_rate": 0.09,
      "total_igtax_amt": 179.8200,
      "error_ind": "N"
    },
    {
      "item_seq_no": 1,
      "igtax_seq_no": 2,
      "tax_authority": "SGST",
      "igtax_code": "GST9",
      "igtax_rate": 0.09,
      "total_igtax_amt": 179.8200,
      "error_ind": "N"
    }
  ]
}
```

### Field name conventions

| Convention | Example | Rule |
|---|---|---|
| Y/N flags | `pos_tran_ind`, `error_ind`, `drop_ship_ind` | Always `"Y"` or `"N"` — never `true`/`false` |
| Monetary | `value`, `unit_retail`, `tender_amt`, `unit_discount_amt` | 4 decimal places (maps to `DECIMAL(20,4)` in Spark) |
| Channel key | `rtlog_orig_sys` | Always `"POS"` for Module 1 |
| JSON item class | `"class"` | ReSA column name — not `"class_"` (Python keyword avoided at serialise time) |
| Date | `business_date` | ISO date string `YYYY-MM-DD` |
| Datetime | `tran_datetime` | ISO 8601 with `+05:30` offset (IST) |

---

## 4. Code walkthrough — file by file

```
pos_simulator/
├── __init__.py          package marker
├── config.py            SimConfig dataclass — all tunable knobs
├── reference_data.py    Static seeds: stores, SKUs, staff, tenders, promotions, tax authorities
├── models.py            RTLOG dataclasses (TranHead, TranItem, TranDisc, TranTender, TranTax,
│                        TranIgtax, RtlogRecord) with to_dict() serialisation
├── generator.py         TransactionGenerator — builds one complete valid RTLOG record per call
├── faults.py            inject_faults() — wraps a record list, corrupts ~fault_rate of them
├── writer.py            write_records() — serialises to NDJSON, creates partition directories
├── main.py              CLI entry point (argparse): `generate` and `sample` subcommands
├── sample_data/         1,000 pre-generated records checked into the repo (seed=42)
│   └── store=*/date=*/hour=*/rtlog.ndjson
├── Dockerfile           Container image for Azure Container Instance deployment
└── run.sh               Local smoke-test script (no Docker needed)
```

---

### `config.py` — `SimConfig`

A plain `@dataclass` — no external dependencies. Every aspect of the simulation is controlled here
so nothing is hard-coded anywhere else.

```python
@dataclass
class SimConfig:
    store_ids: list[int]          # which stores to simulate
    tills_per_store: int          # tills per store
    start_date: date              # simulation start
    end_date: date                # simulation end
    avg_trans_per_till_per_day: int  # VOLUME KNOB — lower this to reduce output
    tax_mode: Literal["IGTAX", "TAX", "BOTH"]
    fault_rate: float             # fraction of bad records (default 0.02 = 2%)
    output_dir: str               # where NDJSON files land
    seed: int | None              # set for reproducible output
    banner_no: int                # multi-banner retailers
    currency: str                 # e.g. "INR"
```

---

### `reference_data.py` — static seeds

Contains everything the generator needs to pick realistic values from:

| Constant | Contents |
|---|---|
| `STORES` | `{store_id: city}` for 50 Indian cities |
| `PRODUCTS` / `SKU_POOL` | 15 product lines × 5 SKUs = 75 SKUs, with dept/class/subclass and base price |
| `TENDER_TYPES` | CASH (30%), CARD (60%), VOUCHER (10%) weighted mix |
| `CASHIER_IDS` | `CASH1001` … `CASH1050` |
| `SALESPERSON_IDS` | `SP0001` … `SP0050` |
| `PROMOTIONS` | 4 promotions (5%–20% discount) |
| `IGTAX_AUTHORITIES` | CGST 9% + SGST 9% (Indian GST split) |
| `ADDITIVE_TAX` | GSTTOT 18% (for `tax_mode="TAX"`) |
| `HOUR_WEIGHTS` | 24-element list — 0 overnight, peaks at lunch (12) and evening (18) |

---

### `models.py` — RTLOG dataclasses

Seven dataclasses, one per ReSA table that the RTLOG bundles:

```
RtlogRecord
  ├── tran_head: TranHead          (maps to SA_TRAN_HEAD, 39 cols)
  ├── tran_item: list[TranItem]    (maps to SA_TRAN_ITEM, 51 cols — subset implemented)
  ├── tran_disc: list[TranDisc]    (maps to SA_TRAN_DISC, 22 cols)
  ├── tran_tender: list[TranTender](maps to SA_TRAN_TENDER, 29 cols)
  ├── tran_tax: list[TranTax]      (maps to SA_TRAN_TAX, 11 cols)
  └── tran_igtax: list[TranIgtax]  (maps to SA_TRAN_IGTAX, 16 cols)
```

`TranItem.to_dict()` handles the `class_` → `"class"` rename so the JSON key matches the Oracle
column name exactly (Python reserves `class` as a keyword; the dataclass field is `class_` but
serialised as `"class"`).

`RtlogRecord.to_dict()` produces a plain `dict` that `json.dumps()` can serialise directly — no
Pydantic, no extra dependencies.

---

### `generator.py` — `TransactionGenerator`

The main workhorse. Public interface:

```python
gen = TransactionGenerator(cfg)

# All transactions for one store, one day
records: list[RtlogRecord] = gen.generate_day(store=1, business_date=date(2024, 1, 15))

# Iterator over a full date range
for record in gen.generate_range(store=1, start=date(2024,1,1), end=date(2024,12,31)):
    ...
```

**How `generate_day` works:**

1. Calculates a volume multiplier for the day:
   - Weekends → ×1.6–1.8
   - Last Friday of November (Black Friday) → ×4.0
2. Opens each till with an `OPEN` transaction at hour 06.
3. Generates `target` transactions, picking `tran_type` by weighted random choice.
4. Closes each till with a `CLOSE` transaction at hour 22.

**How `_build_items` works:**

1. Picks 1–5 random SKUs from `SKU_POOL`.
2. For each SKU, computes `unit_retail` = `base_price ± random(50)`.
3. Appends `SA_TRAN_IGTAX` rows (CGST + SGST) if `tax_mode = "IGTAX"` or `"BOTH"`.
4. Appends `SA_TRAN_TAX` row if `tax_mode = "TAX"` or `"BOTH"`.
5. 25% chance per SALE item of adding a promotion discount to `tran_disc`.

**How `_build_tenders` works:**

Picks a single tender type by weighted choice (CASH 30%, CARD 50–60%, VOUCHER 10%).
For card tenders: generates a masked CC number (`************XXXX`) and auth code.
`tender_amt` is set to exactly equal `tran_head.value` — so all clean records satisfy
audit rule #1 (`TENDER_VAR_GT_01`).

**Temporal realism:**

`_weighted_hour()` uses `HOUR_WEIGHTS` to bias transaction times toward business hours. Combined
with `_day_multiplier()`, the output looks like real retail data when plotted.

---

### `faults.py` — `inject_faults`

Takes a list of clean records and returns a new list with `~fault_rate` of them corrupted.

```python
from pos_simulator.faults import inject_faults
import random

records = inject_faults(records, fault_rate=0.02, rng=random.Random(42))
```

- Original records are **deep-copied** before corruption — the input list is not mutated.
- A `_fault_type` string is attached to the `tran_head.__dict__` for test introspection (not
  serialised to JSON).
- The 6 fault types cycle evenly across all fault slots so each type appears roughly equally often.

See [§5](#5-fault-injection-catalog) for the full fault catalog.

---

### `writer.py` — `write_records`

```python
from pos_simulator.writer import write_records

written: dict[str, int] = write_records(records, output_dir="output/pos_rtlog")
# returns {"/path/to/file.ndjson": record_count, ...}
```

- Creates `store=X/date=YYYY-MM-DD/hour=HH/` directories automatically.
- Writes NDJSON (one JSON object per line, `\n`-separated).
- `append=True` mode is available for incremental runs.
- Hour is extracted from the first 13 chars of `tran_datetime` (`2024-03-15T12` → `"12"`).

---

### `main.py` — CLI

Two subcommands:

```
python -m pos_simulator.main generate [options]
python -m pos_simulator.main sample   [options]
```

See [§7 How to run](#7-how-to-run) for full usage.

---

## 5. Fault injection catalog

~2% of generated records are intentionally corrupted. Each fault type maps to a named Sales Audit
rule that Module 4 will catch and write to `SA_ERROR`.

| # | Fault type | Error code (Module 4) | What is corrupted | Audit rule trigger |
|---|---|---|---|---|
| 1 | `MISSING_TENDER` | `MISSING_TENDER` | `tran_tender` array emptied | Transaction with zero tender rows |
| 2 | `TENDER_VAR_GT_01` | `TENDER_VAR_GT_01` | `tender_amt` inflated by 0.02–5.00 | `|Σ(tender_amt) − value| > 0.01` |
| 3 | `TRAN_NO_DUP` | `TRAN_NO_DUP` | `tran_no` set to match another record on same register | Same `(store, register, tran_no)` twice |
| 4 | `VOID_OOH` | `VOID_OOH` | `tran_type` → `PVOID`, `tran_datetime` hour → 02:00 | PVOID outside 06:00–23:00 |
| 5 | `NEG_QTY_NO_REF` | `NEG_QTY_NO_REF` | `tran_type` → `SALE`, all `qty` → negative, `return_reason_code` → null | `qty < 0` on a SALE with no return reason |
| 6 | `CC_NOT_MASKED` | `CC_NOT_MASKED` | `cc_no` set to 16-digit Visa-range number (unmasked) | CC not starting with mask chars |

**Distribution:** the 6 types cycle evenly, so at `fault_rate=0.02` with ~600 daily records per store
you get approximately 2 records of each fault type per store per day — enough for the audit dashboard
to show a meaningful signal without being noisy.

---

## 6. Configuration reference

All knobs live in `SimConfig`. The most important ones:

| Parameter | Default | Description |
|---|---|---|
| `store_ids` | `[1..50]` | List of store IDs to simulate |
| `tills_per_store` | `4` | How many tills per store |
| `start_date` | `2023-01-01` | First business date |
| `end_date` | `2024-12-31` | Last business date |
| `avg_trans_per_till_per_day` | `150` | **Volume knob.** Reduce to 10–50 for local dev |
| `tax_mode` | `"IGTAX"` | `"IGTAX"` = Indian GST (inclusive), `"TAX"` = additive, `"BOTH"` = both |
| `fault_rate` | `0.02` | Fraction of bad records (0.0 = clean output) |
| `output_dir` | `"output/pos_rtlog"` | Root directory for NDJSON files |
| `seed` | `None` | Integer seed for reproducible output |
| `banner_no` | `1` | Multi-banner retailer banner ID |
| `currency` | `"INR"` | Currency code stored in `ORIG_CURRENCY` |

**Estimated output size at default settings (50 stores × 2 years):**

| Metric | Estimate |
|---|---|
| Total transactions | ~110 M |
| Total transaction lines (items) | ~330 M |
| Compressed NDJSON on disk | ~15–20 GB |

Scale down with `--tpd 10` or `--stores 1,2,3` for local testing.

---

## 7. How to run

### Prerequisites

- Python 3.11+
- Install the package (from repo root):

```bash
pip install -e .
```

### Quick smoke test (3 stores, 3 days)

```bash
python -m pos_simulator.main generate \
    --stores 1,2,3 \
    --start 2024-01-01 \
    --days 3 \
    --tpd 50 \
    --output output/pos_rtlog
```

Or via the shell script:

```bash
bash pos_simulator/run.sh
```

### Full 2-year run (all 50 stores)

```bash
python -m pos_simulator.main generate \
    --start 2023-01-01 \
    --end 2024-12-31 \
    --tpd 150 \
    --seed 42 \
    --output output/pos_rtlog
```

### Generate the 1 000-record sample fixture

```bash
python -m pos_simulator.main sample \
    --output pos_simulator/sample_data
```

### All CLI options

```
usage: pos_simulator generate [-h]
  --stores    STORES      Comma-separated store IDs (default: all 50)
  --start     START       Start date YYYY-MM-DD (default: 2024-01-01)
  --end       END         End date YYYY-MM-DD
  --days      DAYS        Days from start if --end not given (default: 7)
  --tpd       TPD         Avg transactions per till per day (default: 150)
  --fault-rate FAULT_RATE Fraction of bad records (default: 0.02)
  --tax-mode  {IGTAX,TAX,BOTH}  (default: IGTAX)
  --output    OUTPUT      Output directory (default: output/pos_rtlog)
  --seed      SEED        Integer seed for reproducibility
```

### Inspect the output

```bash
# Count total records
python -c "
from pos_simulator.writer import count_output
print(count_output('output/pos_rtlog'))
"

# Pretty-print one record
python -c "
import json
with open('pos_simulator/sample_data/store=1/date=2024-03-15/hour=12/rtlog.ndjson') as f:
    print(json.dumps(json.loads(f.readline()), indent=2))
"
```

---

## 8. Running with Docker

```bash
# Build the image
docker build -f pos_simulator/Dockerfile -t pos-simulator .

# Run: 5 stores, 7 days, output to local ./data/
docker run -v "$(pwd)/data:/data" \
  -e STORES=1,2,3,4,5 \
  -e START_DATE=2024-01-01 \
  -e DAYS=7 \
  -e TPD=150 \
  pos-simulator

# Output lands at ./data/pos_rtlog/store=*/date=*/hour=*/rtlog.ndjson
```

### Environment variables (Docker / ACI)

| Var | Default | Maps to |
|---|---|---|
| `OUTPUT_DIR` | `/data/pos_rtlog` | `--output` |
| `STORES` | `1,2,3,4,5` | `--stores` |
| `START_DATE` | `2024-01-01` | `--start` |
| `DAYS` | `7` | `--days` |
| `TPD` | `150` | `--tpd` |
| `FAULT_RATE` | `0.02` | `--fault-rate` |
| `TAX_MODE` | `IGTAX` | `--tax-mode` |

For Azure Container Instance deployment, mount an ADLS Gen2 container as the `/data` volume using
the blobfuse2 driver and set `OUTPUT_DIR=/data/pos_rtlog`.

---

## 9. Tests

**Location:** `tests/pos_simulator/`
**Framework:** pytest
**Count:** 41 tests, all passing

```bash
# Run all Module 1 tests
pytest tests/pos_simulator/ -v

# Run with coverage
pytest tests/pos_simulator/ --cov=pos_simulator --cov-report=term-missing
```

### Test structure

| File | What it tests |
|---|---|
| `conftest.py` | Shared fixtures: `SimConfig`, `TransactionGenerator`, one day of records |
| `test_generator.py` | Valid record structure: ReSA field names, Y/N flags, 4dp monetary values, CC masking, igtax/tax modes, JSON serialisation |
| `test_faults.py` | Each of the 6 fault types plus fault rate accuracy and record count stability |
| `test_writer.py` | Partition directory structure, NDJSON validity, record count integrity, `"class"` key correctness |

### Key assertions enforced by tests

- `rtlog_orig_sys == "POS"` on every record
- `tran_seq_no` is unique across a day's output
- Tender total matches `tran_head.value` within 0.01 for clean records
- All `*_ind` columns contain only `"Y"` or `"N"`
- All monetary values have exactly 4 decimal places
- Credit card numbers start with `"*"` (masked) on clean records
- `DROP_SHIP_IND = "N"` on all POS items
- JSON key is `"class"` not `"class_"` in item records
- Each fault type produces the expected corruption in the output

---

## 10. Design decisions

| Decision | Rationale |
|---|---|
| **Pure Python standard library** | No Pydantic, no NumPy, no Faker. The simulator installs with zero extra dependencies — keeps the Docker image small and CI fast. |
| **`Decimal` for monetary arithmetic, `float` in output** | `Decimal` prevents rounding drift during multi-item calculations. Values are rounded to 4dp and cast to `float` before serialisation — matching `NUMERIC(20,4)` without imposing a `Decimal` dependency on consumers. |
| **NDJSON (not JSON arrays)** | Auto Loader reads NDJSON natively. A JSON array file would require loading the entire file into memory. NDJSON allows line-by-line streaming. |
| **Hive-style partition paths** | `store=/date=/hour=` is the convention Databricks / Delta Lake uses for partition pruning. Using the same scheme means the Bronze DLT table can be defined with `partitionBy("store", "date", "hour")` and Auto Loader handles new partitions automatically. |
| **`RTLOG_ORIG_SYS` on every record** | All three channels (POS/OMS/MKT) land in the same Silver tables differentiated only by this column. Stamping it at source means the conformance job has nothing to infer. |
| **Fault injection as a post-processing step** | The generator always produces valid records. Faults are applied as a wrapper — this means the generator is independently testable and the fault logic is independently testable. |
| **Deep copy in fault injection** | The original record list is never mutated. This matters because the same record list might be inspected before and after injection in test scenarios. |
| **`_fault_type` test hook on `tran_head.__dict__`** | Attaching metadata to the object without changing the dataclass schema — it's invisible to `to_dict()` / JSON serialisation but readable in tests via `.__dict__`. |
| **Indian GST (IGTAX) as the default tax mode** | The project is built around an Indian retail scenario (50 stores in Indian cities, INR currency). CGST + SGST split into `SA_TRAN_IGTAX` rows is authentic to that market. The `tax_mode` config flag allows switching to additive tax for international scenarios. |
| **Black Friday volume spike (×4)** | Ensures the audit layer is tested under high-volume conditions, not just average days. |

---

## 11. What this feeds downstream

```
pos_simulator/sample_data/          <- Module 3 uses this for unit tests
output/pos_rtlog/                   <- Auto Loader in Module 3 picks this up

Bronze DLT table:  bronze.pos_txn
  - raw RTLOG JSON, append-only, partitioned by ingestion date

Silver conformance job (Module 3):  pos_to_resa
  - reads bronze.pos_txn
  - fans out to: sa_tran_head, sa_tran_item, sa_tran_disc,
                 sa_tran_tender, sa_tran_tax, sa_tran_igtax
  - bad records -> silver._quarantine (rejection_reason)
  - TRAN_NO gaps -> sa_missing_tran

Sales Audit engine (Module 4):
  - reads Silver SA_TRAN_* tables
  - the 6 fault types seeded here trigger rules 1, 2, 3, 6, 5, 11
    from the 18-rule audit catalog
```

---

*End of Module 1 documentation.*
