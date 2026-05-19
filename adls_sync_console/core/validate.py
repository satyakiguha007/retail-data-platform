"""
Validation: compare expected files/stores/dates against what's actually on disk.

For each source kind (pos, marketplace, flat_csv, named_files), implements a check
that returns:
  - file_count
  - found_dimensions (e.g. stores, dates, hours)
  - missing_dimensions (what was expected but absent)
  - alerts (human-readable issues)
"""
from pathlib import Path

from .config import resolve_local_root


def _scan_partitioned_files(local_root: Path, file_name: str) -> dict:
    """
    Walk a partitioned directory (store=N/date=YYYY-MM-DD/hour=NN/file_name)
    and collect all dimension values found.
    """
    found = {"stores": set(), "dates": set(), "hours": set(), "marketplaces": set()}
    files = []
    for f in local_root.rglob(file_name):
        if not f.is_file():
            continue
        files.append(f)
        rel_parts = f.relative_to(local_root).parts
        for part in rel_parts:
            if part.startswith("store="):
                try:
                    found["stores"].add(int(part.split("=", 1)[1]))
                except ValueError:
                    pass
            elif part.startswith("date="):
                found["dates"].add(part.split("=", 1)[1])
            elif part.startswith("hour="):
                try:
                    found["hours"].add(int(part.split("=", 1)[1]))
                except ValueError:
                    pass
            elif part.startswith("marketplace="):
                found["marketplaces"].add(part.split("=", 1)[1])
    return {"files": files, "found": found}


def validate_source(source: dict) -> dict:
    """
    Run validation on a source. Returns a result dict with keys:
      - exists, file_count, total_bytes, found, missing, alerts
    """
    local_root = resolve_local_root(source)
    if not local_root.exists():
        return {
            "exists": False,
            "file_count": 0,
            "total_bytes": 0,
            "found": {},
            "missing": {},
            "alerts": [f"Local path not found: {source['local_root']}"],
        }

    validation = source.get("validation", {})
    kind = validation.get("kind", "flat_csv")

    if kind == "pos":
        scan = _scan_partitioned_files(local_root, "rtlog.ndjson")
        files = scan["files"]
        found = scan["found"]
        missing = {
            "stores": sorted(set(validation.get("stores", [])) - found["stores"]),
            "dates": sorted(set(validation.get("dates", [])) - found["dates"]),
        }
        alerts = []
        if missing["stores"]:
            alerts.append(f"Missing stores: {missing['stores']}")
        if missing["dates"]:
            alerts.append(f"Missing dates: {missing['dates']}")
        return {
            "exists": True,
            "file_count": len(files),
            "total_bytes": sum(f.stat().st_size for f in files),
            "found": {
                "stores": sorted(found["stores"]),
                "dates": sorted(found["dates"]),
                "hours": sorted(found["hours"]),
            },
            "missing": {k: v for k, v in missing.items() if v},
            "alerts": alerts,
        }

    elif kind == "marketplace":
        scan = _scan_partitioned_files(local_root, "feed.ndjson")
        files = scan["files"]
        found = scan["found"]
        missing = {
            "marketplaces": sorted(set(validation.get("marketplaces", [])) - found["marketplaces"]),
            "dates": sorted(set(validation.get("dates", [])) - found["dates"]),
        }
        alerts = []
        if missing["marketplaces"]:
            alerts.append(f"Missing marketplaces: {missing['marketplaces']}")
        if missing["dates"]:
            alerts.append(f"Missing dates: {missing['dates']}")
        return {
            "exists": True,
            "file_count": len(files),
            "total_bytes": sum(f.stat().st_size for f in files),
            "found": {
                "marketplaces": sorted(found["marketplaces"]),
                "dates": sorted(found["dates"]),
            },
            "missing": {k: v for k, v in missing.items() if v},
            "alerts": alerts,
        }

    elif kind == "named_files":
        expected = validation.get("expected_files", [])
        found_files = [f.name for f in local_root.glob("*.csv") if f.is_file()]
        missing_files = sorted(set(expected) - set(found_files))
        all_files = list(local_root.rglob("*"))
        all_files = [f for f in all_files if f.is_file()]
        alerts = []
        if missing_files:
            alerts.append(f"Missing files: {missing_files}")
        if not found_files:
            alerts.append("No CSV files present — folder is empty or contains only docs")
        return {
            "exists": True,
            "file_count": len(all_files),
            "total_bytes": sum(f.stat().st_size for f in all_files),
            "found": {"files": sorted(found_files)},
            "missing": {"files": missing_files} if missing_files else {},
            "alerts": alerts,
        }

    else:  # flat_csv
        all_files = [f for f in local_root.rglob("*") if f.is_file()]
        min_files = validation.get("min_files", 1)
        alerts = []
        if len(all_files) < min_files:
            alerts.append(f"Expected at least {min_files} file(s), found {len(all_files)}")
        return {
            "exists": True,
            "file_count": len(all_files),
            "total_bytes": sum(f.stat().st_size for f in all_files),
            "found": {"files": [f.name for f in all_files]},
            "missing": {},
            "alerts": alerts,
        }
