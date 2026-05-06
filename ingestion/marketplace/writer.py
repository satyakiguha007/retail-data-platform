"""Writer: serialises MktOrder lists to partitioned NDJSON files.

Partition layout mirrors pos_simulator for Auto Loader consistency:
  {output_dir}/marketplace={AMAZON_IN}/date=2023-01-03/feed.ndjson
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .models import MktOrder


def write_feed(
    orders_by_mkt: dict[str, list[MktOrder]],
    settle_date: date,
    output_dir: str,
) -> list[Path]:
    """Write one NDJSON file per marketplace for the given settle_date.

    Returns the list of files written.
    """
    written: list[Path] = []
    date_str = settle_date.isoformat()

    for mkt_name, orders in orders_by_mkt.items():
        if not orders:
            continue
        out = Path(output_dir) / f"marketplace={mkt_name}" / f"date={date_str}" / "feed.ndjson"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            for order in orders:
                fh.write(json.dumps(order.to_dict(), ensure_ascii=False) + "\n")
        written.append(out)

    return written
