"""generate_fx_rates.py

Synthetic daily FX rates for the retail-data-platform simulation.
Produces one row per currency pair per calendar day for the requested range.

Method: geometric Brownian motion (random walk) around historically-grounded
base rates. AED is treated as a near-peg (very low vol).

Output schema:
  rate_date     - ISO date string (YYYY-MM-DD)
  from_currency - ISO 4217 code (INR / GBP / AED / SGD / USD)
  to_currency   - always 'USD' (reporting currency for this platform)
  rate          - DECIMAL(18,6): 1 unit of from_currency = rate USD

Usage:
  py -3 generate_fx_rates.py
  py -3 generate_fx_rates.py --start 2023-01-01 --end 2024-12-31 --seed 42
  py -3 generate_fx_rates.py --out output/fx_rates.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import random
from datetime import date, timedelta
from pathlib import Path

START = date(2023, 1, 1)
END = date(2024, 12, 31)

# 1 unit of local currency = _BASE_RATES[ccy] USD on START date
# Grounded in approximate Jan-2023 spot rates
_BASE_RATES: dict[str, float] = {
    "INR": 1 / 82.50,   # 1 INR ≈ 0.01212 USD  (1 USD ≈ 82.50 INR)
    "GBP": 1 / 0.830,   # 1 GBP ≈ 1.20482 USD  (1 USD ≈ 0.83 GBP)
    "AED": 1 / 3.6725,  # 1 AED ≈ 0.27229 USD  (USD/AED semi-peg)
    "SGD": 1 / 1.340,   # 1 SGD ≈ 0.74627 USD  (1 USD ≈ 1.34 SGD)
    "USD": 1.0,
}

# Annual volatility %. Daily vol = annual / sqrt(252).
_ANNUAL_VOL: dict[str, float] = {
    "INR": 0.040,  # low: RBI-managed float
    "GBP": 0.080,  # higher: active liquid pair
    "AED": 0.001,  # near-zero: quasi-peg to USD
    "SGD": 0.045,  # moderate: MAS-managed float
    "USD": 0.000,
}

# Annual log-drift. Negative = depreciation vs USD.
_ANNUAL_DRIFT: dict[str, float] = {
    "INR": -0.018,  # gentle INR depreciation
    "GBP":  0.000,
    "AED":  0.000,
    "SGD":  0.005,  # slight SGD appreciation
    "USD":  0.000,
}


def generate(start: date, end: date, seed: int | None = 42) -> list[dict]:
    """Return list of dicts, one per (date × currency pair)."""
    rng = random.Random(seed)
    dt = 1 / 252  # one calendar day as fraction of trading year

    rows: list[dict] = []
    # log-price state for each currency (initialised from base rates)
    log_rates = {ccy: math.log(r) for ccy, r in _BASE_RATES.items()}

    current = start
    while current <= end:
        for ccy in _BASE_RATES:
            if ccy == "USD":
                rate = 1.0
            else:
                annual_vol = _ANNUAL_VOL[ccy]
                daily_vol = annual_vol / math.sqrt(252)
                drift = (_ANNUAL_DRIFT[ccy] - 0.5 * annual_vol ** 2) * dt
                shock = rng.gauss(0, 1) * daily_vol
                log_rates[ccy] += drift + shock
                rate = math.exp(log_rates[ccy])

            rows.append({
                "rate_date": current.isoformat(),
                "from_currency": ccy,
                "to_currency": "USD",
                "rate": round(rate, 6),
            })
        current += timedelta(days=1)

    return rows


def write_csv(rows: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["rate_date", "from_currency", "to_currency", "rate"]
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows):,} rows -> {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic daily FX rates CSV")
    parser.add_argument("--start", default=START.isoformat(), metavar="YYYY-MM-DD")
    parser.add_argument("--end",   default=END.isoformat(),   metavar="YYYY-MM-DD")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).parent / "sample_data" / "fx_rates_2023_2024.csv"),
        metavar="PATH",
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rows = generate(
        date.fromisoformat(args.start),
        date.fromisoformat(args.end),
        args.seed,
    )
    write_csv(rows, Path(args.out))


if __name__ == "__main__":
    main()
