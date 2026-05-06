"""Fetch real daily FX rates from the Frankfurter API (European Central Bank data).

API: https://api.frankfurter.app  — free, no API key, no rate limit for normal use.

What it returns:
  Rates quoted as "1 USD = X local currency" (e.g. 1 USD = 82.5 INR).
  We invert these to match our schema: "1 local = Y USD" (e.g. 1 INR = 0.01212 USD).

Important caveat:
  The ECB only publishes rates on trading days (weekdays, excluding major holidays).
  This fetcher forward-fills missing calendar days (weekends / bank holidays) using
  the most recent available rate — the standard practice for daily FX lookups.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

# Base URL — no auth required
_BASE_URL = "https://api.frankfurter.app"

# Currencies we need (all quoted against USD as base)
_TARGET_CURRENCIES = ["INR", "GBP", "AED", "SGD"]

# How long to wait for the server before giving up (seconds)
_TIMEOUT_SECONDS = 30


def fetch(start: date, end: date) -> list[dict]:
    """Fetch daily FX rates for the given date range.

    Makes a single HTTP GET request to Frankfurter.  If the range spans more
    than ~1 year the response can be a few hundred KB — still one request.

    Parameters
    ----------
    start, end : date
        Inclusive date range to fetch.

    Returns
    -------
    list of dicts with keys: rate_date, from_currency, to_currency, rate
    Same format as generate_fx_rates.generate() so callers are interchangeable.

    Raises
    ------
    urllib.error.URLError   if the network is unreachable
    urllib.error.HTTPError  if the API returns a non-200 status
    ValueError              if the response JSON has an unexpected structure
    """
    # Build the URL
    # Example: https://api.frankfurter.app/2023-01-01..2024-12-31?from=USD&to=INR,GBP,AED,SGD
    targets = ",".join(_TARGET_CURRENCIES)
    url = f"{_BASE_URL}/{start.isoformat()}..{end.isoformat()}?from=USD&to={targets}"

    print(f"  Calling: {url}")

    # Make the HTTP request
    req = urllib.request.Request(url, headers={"User-Agent": "retail-data-platform/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as response:
        raw = response.read()

    # Parse JSON
    data = json.loads(raw)

    # Validate that the expected keys are present
    if "rates" not in data or not isinstance(data["rates"], dict):
        raise ValueError(f"Unexpected API response structure: {list(data.keys())}")

    # data["rates"] looks like:
    #   {"2023-01-02": {"INR": 82.685, "GBP": 0.8333, "AED": 3.6725, "SGD": 1.3371}, ...}
    # Note: key "2023-01-01" is missing (New Year's Day holiday) — that's normal.

    api_rates: dict[str, dict[str, float]] = data["rates"]

    # Forward-fill: walk every calendar day and use the last known rate
    # on weekends / holidays where the API has no data.
    rows: list[dict] = []
    last_known: dict[str, float] = {}   # currency → latest USD-denominated rate

    current = start
    while current <= end:
        date_str = current.isoformat()

        if date_str in api_rates:
            last_known = api_rates[date_str]    # update with today's traded rates

        if last_known:
            # Convert from "1 USD = X local" to "1 local = Y USD"
            for ccy, usd_per_local_inv in last_known.items():
                rows.append({
                    "rate_date":     date_str,
                    "from_currency": ccy,
                    "to_currency":   "USD",
                    "rate":          round(1.0 / usd_per_local_inv, 6),
                })

        # USD → USD is always 1
        rows.append({
            "rate_date":     date_str,
            "from_currency": "USD",
            "to_currency":   "USD",
            "rate":          1.0,
        })

        current += timedelta(days=1)

    actual_trading_days = len(api_rates)
    calendar_days = (end - start).days + 1
    print(f"  Received {actual_trading_days} trading days, forward-filled to {calendar_days} calendar days.")
    print(f"  Total rows: {len(rows):,}")

    return rows
