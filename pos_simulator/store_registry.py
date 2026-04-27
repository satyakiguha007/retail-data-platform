"""Store registry — loads store metadata from stores.csv.

Each row in stores.csv becomes a StoreRecord, keyed by store_no.
The generator uses this for per-store tills and local currency instead of
global config defaults.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_CSV = Path(__file__).parent / "stores.csv"


@dataclass(frozen=True)
class StoreRecord:
    store_no: int
    store_name: str
    tills: int
    country: str
    local_currency: str
    exchange_rate_to_usd: float


def load_stores(csv_path: str | Path | None = None) -> dict[int, StoreRecord]:
    """Load store metadata from a CSV file.

    Returns a dict keyed by store_no. Uses the bundled stores.csv by default.
    """
    path = Path(csv_path) if csv_path else _DEFAULT_CSV
    stores: dict[int, StoreRecord] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            record = StoreRecord(
                store_no=int(row["store_no"]),
                store_name=row["store_name"].strip(),
                tills=int(row["tills"]),
                country=row["country"].strip(),
                local_currency=row["local_currency"].strip(),
                exchange_rate_to_usd=float(row["exchange_rate_to_usd"]),
            )
            stores[record.store_no] = record
    return stores
