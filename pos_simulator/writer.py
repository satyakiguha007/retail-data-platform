"""RTLOG writer — serialises RtlogRecords to NDJSON files.

Output structure (mirrors ADLS landing zone partitioning):
  {output_dir}/store={store_id}/date={YYYY-MM-DD}/hour={HH}/rtlog.ndjson

Each file is newline-delimited JSON (one record per line), matching what
Azure Auto Loader ingests with cloudFiles format 'json'.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

from .models import RtlogRecord


def _partition_key(record: RtlogRecord) -> tuple[int, str, str]:
    """Return (store, date_str, hour_str) for partition path."""
    head = record.tran_head
    dt_str = head.tran_datetime  # ISO datetime with offset
    hour = dt_str[11:13]  # HH characters
    return (head.store, head.business_date, hour.zfill(2))


def write_records(
    records: list[RtlogRecord],
    output_dir: str | Path,
    append: bool = False,
) -> dict[str, int]:
    """Write records to partitioned NDJSON files.

    Returns a dict of {file_path: record_count} for reporting.
    """
    output_dir = Path(output_dir)
    buckets: dict[tuple, list[RtlogRecord]] = defaultdict(list)

    for record in records:
        buckets[_partition_key(record)].append(record)

    written: dict[str, int] = {}
    mode = "a" if append else "w"

    for (store, date_str, hour_str), recs in buckets.items():
        partition_path = (
            output_dir
            / f"store={store}"
            / f"date={date_str}"
            / f"hour={hour_str}"
        )
        partition_path.mkdir(parents=True, exist_ok=True)
        file_path = partition_path / "rtlog.ndjson"

        with open(file_path, mode, encoding="utf-8") as fh:
            for rec in recs:
                fh.write(json.dumps(rec.to_dict(), ensure_ascii=False))
                fh.write("\n")

        written[str(file_path)] = written.get(str(file_path), 0) + len(recs)

    return written


def count_output(output_dir: str | Path) -> int:
    """Count total records across all NDJSON files in an output directory."""
    total = 0
    for path in Path(output_dir).rglob("*.ndjson"):
        with open(path, encoding="utf-8") as fh:
            total += sum(1 for line in fh if line.strip())
    return total
