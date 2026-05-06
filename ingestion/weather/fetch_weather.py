"""Fetch real daily weather observations from Open-Meteo (ERA5 reanalysis data).

API: https://open-meteo.com  — free, no API key, historical data back to 1940.
Endpoint used: archive-api.open-meteo.com/v1/archive (historical only).

What we fetch per city:
  - temperature_2m_max   (°C daily maximum)
  - temperature_2m_min   (°C daily minimum)
  - precipitation_sum    (mm of rain/snow in the day)
  - weathercode          (WMO code — we map this to our CONDITION labels)

One API call per unique city.  We have 26 stores across ~20 cities, so the
total request count is small (≤20 calls, ~0.3 s each).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

_BASE_URL = "https://archive-api.open-meteo.com/v1/archive"
_TIMEOUT_SECONDS = 30
_POLITE_DELAY_SECONDS = 0.4    # small pause between city calls — good API citizenship

# ---------------------------------------------------------------------------
# Store → city coordinates
# Matches the city assignments in generate_weather.py _STORE_CLIMATE dict.
# ---------------------------------------------------------------------------
_STORE_COORDS: dict[int, tuple[str, str, float, float]] = {
    # store_no: (city, country, latitude, longitude)
    33487: ("Kolkata",      "India",     22.5726,  88.3639),
    39876: ("Mumbai",       "India",     19.0760,  72.8777),
    41203: ("Delhi",        "India",     28.7041,  77.1025),
    44512: ("Bangalore",    "India",     12.9716,  77.5946),
    47889: ("Chennai",      "India",     13.0827,  80.2707),
    51234: ("Hyderabad",    "India",     17.3850,  78.4867),
    53901: ("Kochi",        "India",      9.9312,  76.2673),
    56123: ("Bangalore",    "India",     12.9716,  77.5946),  # same coords as 44512
    58745: ("Ahmedabad",    "India",     23.0225,  72.5714),
    62003: ("Delhi",        "India",     28.7041,  77.1025),  # same as 41203
    64301: ("Chandigarh",   "India",     30.7333,  76.7794),
    66782: ("Mumbai",       "India",     19.0760,  72.8777),  # same as 39876
    70001: ("Los Angeles",  "USA",       34.0522, -118.2437),
    70002: ("Minneapolis",  "USA",       44.9778,  -93.2650),
    70003: ("Dallas",       "USA",       32.7767,  -96.7970),
    70004: ("Washington DC","USA",       38.9072,  -77.0369),
    70005: ("Philadelphia", "USA",       39.9526,  -75.1652),
    80001: ("London",       "UK",        51.5074,   -0.1278),
    80002: ("London",       "UK",        51.5074,   -0.1278),  # same as 80001
    80003: ("Manchester",   "UK",        53.4808,   -2.2426),
    80004: ("Sheffield",    "UK",        53.3811,   -1.4701),
    90001: ("Dubai",        "UAE",       25.2048,   55.2708),
    90002: ("Dubai",        "UAE",       25.2048,   55.2708),  # same as 90001
    90003: ("Abu Dhabi",    "UAE",       24.4539,   54.3773),
    95001: ("Singapore",    "Singapore",  1.3521,  103.8198),
    95002: ("Singapore",    "Singapore",  1.3521,  103.8198),  # same as 95001
}

# ---------------------------------------------------------------------------
# WMO weather code → our CONDITION label
# Full table: https://open-meteo.com/en/docs#weathervariables
# ---------------------------------------------------------------------------
def _wmo_to_condition(code: int | None) -> str:
    if code is None:
        return "SUNNY"
    if code == 0:                       return "SUNNY"
    if code == 1:                       return "SUNNY"
    if code == 2:                       return "PARTLY_CLOUDY"
    if code == 3:                       return "CLOUDY"
    if code in (45, 48):                return "CLOUDY"          # fog
    if 51 <= code <= 67:                return "RAINY"           # drizzle / rain
    if 71 <= code <= 77:                return "SNOWY"           # snow / sleet
    if 80 <= code <= 82:                return "RAINY"           # rain showers
    if code in (85, 86):                return "SNOWY"           # snow showers
    if code in (95, 96, 99):            return "STORMY"          # thunderstorm
    return "CLOUDY"                                              # anything else → cloudy


# ---------------------------------------------------------------------------
# Single-city fetch
# ---------------------------------------------------------------------------
def _fetch_city(lat: float, lon: float, start: date, end: date) -> dict:
    """Call Open-Meteo for one city.  Returns the parsed JSON response dict."""
    params = "&".join([
        f"latitude={lat}",
        f"longitude={lon}",
        f"start_date={start.isoformat()}",
        f"end_date={end.isoformat()}",
        "daily=temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode",
        "timezone=auto",            # server picks the local timezone for the coordinates
    ])
    url = f"{_BASE_URL}?{params}"

    req = urllib.request.Request(url, headers={"User-Agent": "retail-data-platform/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------
def fetch(start: date, end: date) -> list[dict]:
    """Fetch daily weather for all 26 stores from the Open-Meteo API.

    Makes one API call per unique (lat, lon) coordinate.  Stores that share
    a city (e.g. both Delhi stores) reuse the same API response.

    Returns
    -------
    list of dicts with keys:
        obs_date, store_no, city, country,
        temp_max_c, temp_min_c, precipitation_mm, condition
    Same format as generate_weather.generate() so callers are interchangeable.

    Raises
    ------
    urllib.error.URLError / HTTPError on network or API problems.
    ValueError if the response is missing expected fields.
    """
    # Deduplicate: map (lat, lon) → list of store_nos that share those coords
    coord_to_stores: dict[tuple[float, float], list[tuple[int, str, str]]] = {}
    for store_no, (city, country, lat, lon) in _STORE_COORDS.items():
        key = (lat, lon)
        coord_to_stores.setdefault(key, []).append((store_no, city, country))

    rows: list[dict] = []
    total_cities = len(coord_to_stores)

    for idx, ((lat, lon), store_list) in enumerate(coord_to_stores.items(), start=1):
        city_name = store_list[0][1]
        print(f"  [{idx}/{total_cities}] Fetching {city_name} ({lat}, {lon})...")

        data = _fetch_city(lat, lon, start, end)

        # Validate response structure
        daily = data.get("daily")
        if not daily or "time" not in daily:
            raise ValueError(f"Unexpected response for {city_name}: {list(data.keys())}")

        dates         = daily["time"]
        temp_max_list = daily.get("temperature_2m_max", [])
        temp_min_list = daily.get("temperature_2m_min", [])
        precip_list   = daily.get("precipitation_sum",  [])
        code_list     = daily.get("weathercode",        [])

        # For each date in the response, emit one row per store sharing this city
        for i, obs_date_str in enumerate(dates):
            temp_max = temp_max_list[i] if i < len(temp_max_list) else None
            temp_min = temp_min_list[i] if i < len(temp_min_list) else None
            precip   = precip_list[i]   if i < len(precip_list)   else 0.0
            wmo_code = code_list[i]     if i < len(code_list)     else None

            condition = _wmo_to_condition(int(wmo_code) if wmo_code is not None else None)

            for store_no, city, country in store_list:
                rows.append({
                    "obs_date":         obs_date_str,
                    "store_no":         store_no,
                    "city":             city,
                    "country":          country,
                    "temp_max_c":       round(temp_max, 1) if temp_max is not None else None,
                    "temp_min_c":       round(temp_min, 1) if temp_min is not None else None,
                    "precipitation_mm": round(precip, 1) if precip is not None else 0.0,
                    "condition":        condition,
                })

        # Small polite delay so we don't hammer the API
        if idx < total_cities:
            time.sleep(_POLITE_DELAY_SECONDS)

    print(f"  Fetched {len(rows):,} rows for {len(coord_to_stores)} unique cities / 26 stores.")
    return rows
