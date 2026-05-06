"""Interactive entry point for weather data.

Pops up a UI asking the user to choose:
  1. Fetch real historical data from Open-Meteo (ERA5 climate reanalysis)
  2. Generate synthetic data (seasonal climate profiles)

If real-time is chosen and fails, the user gets another popup:
  1. Try again
  2. Fall back to synthetic data

Usage
-----
  py -3 ingestion/weather/run_weather.py
  py -3 ingestion/weather/run_weather.py --start 2024-01-01 --end 2026-05-06
  py -3 ingestion/weather/run_weather.py --out output/weather.csv
  py -3 ingestion/weather/run_weather.py --mode realtime    # skip popup
  py -3 ingestion/weather/run_weather.py --mode synthetic   # skip popup
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ingestion.ui_selector import ask_retry_or_fallback, ask_source, run_with_loading
from ingestion.weather.fetch_weather import fetch as fetch_realtime
from ingestion.weather.generate_weather import generate as generate_synthetic, write_csv

_DEFAULT_START = date(2023, 1, 1)
_DEFAULT_END   = date(2024, 12, 31)
_DEFAULT_OUT   = Path(__file__).parent / "sample_data" / "weather_2023_2024.csv"


def run(start: date, end: date, out_path: Path, mode: str | None = None) -> None:
    """Orchestrate the full fetch-or-generate-then-save pipeline.

    Parameters
    ----------
    start, end : date
        Inclusive date range.
    out_path : Path
        Where to write the output CSV.
    mode : "realtime" | "synthetic" | None
        Pass a mode to skip the popup.  None = show the popup.
    """
    date_range = f"{start} to {end}"

    # --- Step 1: ask the user (or use --mode flag) ---
    source = mode if mode in ("realtime", "synthetic") else ask_source("Weather", date_range)

    # --- Step 2: fetch or generate, with retry loop on API failure ---
    rows = None

    if source == "synthetic":
        print("Generating synthetic weather data...")
        rows = generate_synthetic(start, end)

    else:  # realtime — retry loop
        while rows is None:
            try:
                # Note: the weather fetch calls ~20 API endpoints sequentially,
                # one per unique city. This can take 10-15 seconds.
                rows = run_with_loading(
                    task_fn=lambda: fetch_realtime(start, end),
                    message="Fetching weather from Open-Meteo API (~20 cities)...",
                )
            except Exception as exc:
                print(f"  API error: {exc}")
                decision = ask_retry_or_fallback(str(exc))

                if decision == "retry":
                    print("  Retrying...")
                else:
                    print("  Falling back to synthetic generation...")
                    rows = generate_synthetic(start, end)

    # --- Step 3: write CSV ---
    write_csv(rows, out_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch or generate daily weather data and save to CSV."
    )
    parser.add_argument(
        "--start", default=_DEFAULT_START.isoformat(),
        help="Start date YYYY-MM-DD  (default: 2023-01-01)",
    )
    parser.add_argument(
        "--end", default=_DEFAULT_END.isoformat(),
        help="End date YYYY-MM-DD  (default: 2024-12-31)",
    )
    parser.add_argument(
        "--out", default=str(_DEFAULT_OUT),
        help="Output CSV file path",
        metavar="PATH",
    )
    parser.add_argument(
        "--mode", choices=["realtime", "synthetic"], default=None,
        help="Skip the popup and go straight to this mode",
    )
    args = parser.parse_args()

    run(
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        out_path=Path(args.out),
        mode=args.mode,
    )


if __name__ == "__main__":
    main()
