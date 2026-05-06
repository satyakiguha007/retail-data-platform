"""generate_weather.py

Synthetic daily weather data for retail store cities.

One row per store per day. Drives weather-basket analysis in the Gold/serving layer
(e.g. umbrella sales spike on rainy days, ice cream on hot days).

Output schema:
  obs_date          - YYYY-MM-DD
  store_no          - int (FK to bronze.stores / stores.csv)
  city              - string
  country           - string
  temp_max_c        - float (daily max temperature, Celsius)
  temp_min_c        - float (daily min temperature, Celsius)
  precipitation_mm  - float (daily rainfall, mm; 0 = dry)
  condition         - string: SUNNY / PARTLY_CLOUDY / CLOUDY / RAINY / STORMY / SNOWY

Usage:
  py -3 generate_weather.py
  py -3 generate_weather.py --start 2023-01-01 --end 2024-12-31 --out output/weather.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import random
from datetime import date, timedelta
from pathlib import Path

# City climate profiles — (mean_temp_jan, mean_temp_jul, annual_precip_mm, precip_seasonality)
# precip_seasonality > 0 => wetter in summer; < 0 => wetter in winter (Mediterranean)
_STORE_CLIMATE: dict[int, dict] = {
    # India — hot tropical climate
    33487: {"city": "Kolkata",   "country": "India",     "mean_jan": 19, "mean_jul": 30, "ann_precip": 1600, "season": 1.5},
    39876: {"city": "Mumbai",    "country": "India",     "mean_jan": 24, "mean_jul": 29, "ann_precip": 2400, "season": 2.0},
    41203: {"city": "Delhi",     "country": "India",     "mean_jan": 14, "mean_jul": 32, "ann_precip": 800,  "season": 1.5},
    44512: {"city": "Bangalore", "country": "India",     "mean_jan": 21, "mean_jul": 24, "ann_precip": 900,  "season": 1.0},
    47889: {"city": "Chennai",   "country": "India",     "mean_jan": 26, "mean_jul": 30, "ann_precip": 1400, "season": 0.5},
    51234: {"city": "Hyderabad", "country": "India",     "mean_jan": 22, "mean_jul": 28, "ann_precip": 900,  "season": 1.2},
    53901: {"city": "Kochi",     "country": "India",     "mean_jan": 27, "mean_jul": 27, "ann_precip": 3100, "season": 1.8},
    56123: {"city": "Bangalore", "country": "India",     "mean_jan": 21, "mean_jul": 24, "ann_precip": 900,  "season": 1.0},
    58745: {"city": "Ahmedabad", "country": "India",     "mean_jan": 20, "mean_jul": 32, "ann_precip": 780,  "season": 1.6},
    62003: {"city": "Delhi",     "country": "India",     "mean_jan": 14, "mean_jul": 32, "ann_precip": 800,  "season": 1.5},
    64301: {"city": "Chandigarh","country": "India",     "mean_jan": 12, "mean_jul": 30, "ann_precip": 1100, "season": 1.3},
    66782: {"city": "Mumbai",    "country": "India",     "mean_jan": 24, "mean_jul": 29, "ann_precip": 2400, "season": 2.0},
    # USA — temperate/varied
    70001: {"city": "Los Angeles","country": "USA",      "mean_jan": 14, "mean_jul": 24, "ann_precip": 380,  "season": -1.0},
    70002: {"city": "Minneapolis","country": "USA",      "mean_jan": -9, "mean_jul": 23, "ann_precip": 790,  "season": 0.5},
    70003: {"city": "Dallas",    "country": "USA",       "mean_jan": 9,  "mean_jul": 34, "ann_precip": 950,  "season": 0.3},
    70004: {"city": "Washington DC","country": "USA",    "mean_jan": 3,  "mean_jul": 28, "ann_precip": 1050, "season": 0.2},
    70005: {"city": "Philadelphia","country": "USA",     "mean_jan": 1,  "mean_jul": 27, "ann_precip": 1100, "season": 0.2},
    # UK — cool maritime
    80001: {"city": "London",    "country": "UK",        "mean_jan": 5,  "mean_jul": 19, "ann_precip": 600,  "season": -0.3},
    80002: {"city": "London",    "country": "UK",        "mean_jan": 5,  "mean_jul": 19, "ann_precip": 600,  "season": -0.3},
    80003: {"city": "Manchester","country": "UK",        "mean_jan": 4,  "mean_jul": 17, "ann_precip": 810,  "season": -0.2},
    80004: {"city": "Sheffield", "country": "UK",        "mean_jan": 4,  "mean_jul": 17, "ann_precip": 750,  "season": -0.2},
    # UAE — hot arid
    90001: {"city": "Dubai",     "country": "UAE",       "mean_jan": 19, "mean_jul": 36, "ann_precip": 90,   "season": -0.5},
    90002: {"city": "Dubai",     "country": "UAE",       "mean_jan": 19, "mean_jul": 36, "ann_precip": 90,   "season": -0.5},
    90003: {"city": "Abu Dhabi", "country": "UAE",       "mean_jan": 19, "mean_jul": 36, "ann_precip": 80,   "season": -0.5},
    # Singapore — equatorial
    95001: {"city": "Singapore", "country": "Singapore", "mean_jan": 27, "mean_jul": 28, "ann_precip": 2200, "season": 0.3},
    95002: {"city": "Singapore", "country": "Singapore", "mean_jan": 27, "mean_jul": 28, "ann_precip": 2200, "season": 0.3},
}

_CONDITIONS = ["SUNNY", "PARTLY_CLOUDY", "CLOUDY", "RAINY", "STORMY", "SNOWY"]


def _seasonal_mean(mean_jan: float, mean_jul: float, month: int) -> float:
    """Sinusoidal interpolation between January and July means."""
    angle = math.pi * (month - 1) / 6  # 0 at Jan, π at Jul
    return mean_jan + (mean_jul - mean_jan) * (1 - math.cos(angle)) / 2


def _precip_for_day(rng: random.Random, ann_precip: float, season: float, month: int) -> float:
    """Sample daily precipitation in mm."""
    # Monthly precip factor: season>0 peaks in Jul, season<0 peaks in Jan
    angle = math.pi * (month - 1) / 6
    factor = 1.0 + season * math.sin(angle)
    monthly_mean = (ann_precip / 365) * 30 * max(0.1, factor)
    # Probability it rains at all (Poisson-ish: p = 1 - exp(-monthly_mean/30/5))
    p_rain = 1 - math.exp(-monthly_mean / 30 / 5)
    if rng.random() > p_rain:
        return 0.0
    # Gamma-distributed amount when it does rain
    return round(rng.gammavariate(1.2, monthly_mean / 30 / 1.2), 1)


def _condition(temp: float, precip: float, rng: random.Random) -> str:
    if precip > 20:
        return "STORMY"
    if precip > 5:
        return "RAINY"
    if temp < 0 and rng.random() < 0.5:
        return "SNOWY"
    if precip > 0:
        return "CLOUDY"
    return rng.choice(["SUNNY", "PARTLY_CLOUDY", "SUNNY"])  # bias toward sunny when dry


def generate(start: date, end: date, seed: int | None = 42) -> list[dict]:
    rng = random.Random(seed)
    rows: list[dict] = []
    current = start

    while current <= end:
        month = current.month
        for store_no, climate in _STORE_CLIMATE.items():
            mean = _seasonal_mean(climate["mean_jan"], climate["mean_jul"], month)
            daily_sd = 4.0
            temp_max = round(mean + abs(rng.gauss(0, daily_sd)), 1)
            temp_min = round(temp_max - rng.uniform(5, 12), 1)
            precip   = _precip_for_day(rng, climate["ann_precip"], climate["season"], month)
            temp_avg = (temp_max + temp_min) / 2
            rows.append({
                "obs_date":         current.isoformat(),
                "store_no":         store_no,
                "city":             climate["city"],
                "country":          climate["country"],
                "temp_max_c":       temp_max,
                "temp_min_c":       temp_min,
                "precipitation_mm": precip,
                "condition":        _condition(temp_avg, precip, rng),
            })
        current += timedelta(days=1)

    return rows


def write_csv(rows: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["obs_date", "store_no", "city", "country",
              "temp_max_c", "temp_min_c", "precipitation_mm", "condition"]
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows):,} rows -> {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic daily weather data")
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end",   default="2024-12-31")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).parent / "sample_data" / "weather_2023_2024.csv"),
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rows = generate(date.fromisoformat(args.start), date.fromisoformat(args.end), args.seed)
    write_csv(rows, Path(args.out))


if __name__ == "__main__":
    main()
