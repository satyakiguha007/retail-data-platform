"""CLI entry point for the POS RTLOG Simulator.

Usage examples:
  # 5 stores, 7 days (quick local test)
  python -m pos_simulator.main generate --stores 1,2,3,4,5 --start 2024-01-01 --days 7

  # Full 2-year run, all 50 stores, seed for reproducibility
  python -m pos_simulator.main generate --start 2023-01-01 --end 2024-12-31 --seed 42

  # Scale down volume (useful when Azure costs pinch)
  python -m pos_simulator.main generate --days 30 --tpd 50

  # Generate 1K sample records for fixture/testing
  python -m pos_simulator.main sample --output pos_simulator/sample_data
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from .config import SimConfig
from .faults import inject_faults
from .generator import TransactionGenerator
from .writer import count_output, write_records


def _parse_stores(stores_arg: str | None, n: int = 50) -> list[int]:
    if stores_arg:
        return [int(s.strip()) for s in stores_arg.split(",")]
    return list(range(1, n + 1))


def cmd_generate(args: argparse.Namespace) -> None:
    start = date.fromisoformat(args.start)
    if args.end:
        end = date.fromisoformat(args.end)
    else:
        end = start + timedelta(days=args.days - 1)

    store_ids = _parse_stores(args.stores)
    cfg = SimConfig(
        store_ids=store_ids,
        start_date=start,
        end_date=end,
        avg_trans_per_till_per_day=args.tpd,
        fault_rate=args.fault_rate,
        output_dir=args.output,
        seed=args.seed,
        tax_mode=args.tax_mode,
    )

    import random
    rng = random.Random(cfg.seed)
    gen = TransactionGenerator(cfg)

    total_written = 0
    for store in cfg.store_ids:
        print(f"  Generating store {store} …", end=" ", flush=True)
        records = []
        current = cfg.start_date
        while current <= cfg.end_date:
            records.extend(gen.generate_day(store, current))
            current += timedelta(days=1)

        records = inject_faults(records, cfg.fault_rate, rng)
        written = write_records(records, cfg.output_dir)
        n = sum(written.values())
        total_written += n
        print(f"{n:,} records -> {len(written)} files")

    print(f"\nDone. Total records: {total_written:,}")
    print(f"Output: {cfg.output_dir}")


def cmd_sample(args: argparse.Namespace) -> None:
    """Generate exactly 1 000 records across 3 stores × 1 day for fixtures."""
    import random as _random

    cfg = SimConfig(
        store_ids=[1, 2, 3],
        start_date=date(2024, 3, 15),
        end_date=date(2024, 3, 15),
        avg_trans_per_till_per_day=90,   # ~3 stores × 90 × 4 tills ≈ 1 080 records
        fault_rate=0.02,
        output_dir=args.output,
        seed=42,
    )
    rng = _random.Random(42)
    gen = TransactionGenerator(cfg)

    all_records = []
    for store in cfg.store_ids:
        all_records.extend(gen.generate_day(store, cfg.start_date))

    all_records = all_records[:1000]
    all_records = inject_faults(all_records, cfg.fault_rate, rng)
    written = write_records(all_records, cfg.output_dir)
    print(f"Sample: {sum(written.values())} records -> {args.output}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pos_simulator",
        description="POS RTLOG Simulator — generates Oracle ReSA-shaped transaction data",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- generate ---
    gen_p = sub.add_parser("generate", help="Generate RTLOG records for a date range")
    gen_p.add_argument("--stores", default=None,
                       help="Comma-separated store IDs (default: all 50)")
    gen_p.add_argument("--start", default="2024-01-01", help="Start date YYYY-MM-DD")
    gen_p.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    gen_p.add_argument("--days", type=int, default=7,
                       help="Number of days if --end not given (default: 7)")
    gen_p.add_argument("--tpd", type=int, default=150,
                       help="Avg transactions per till per day (volume knob, default: 150)")
    gen_p.add_argument("--fault-rate", type=float, default=0.02,
                       help="Fraction of bad records (default: 0.02)")
    gen_p.add_argument("--tax-mode", choices=["IGTAX", "TAX", "BOTH"], default="IGTAX")
    gen_p.add_argument("--output", default="output/pos_rtlog")
    gen_p.add_argument("--seed", type=int, default=None)
    gen_p.set_defaults(func=cmd_generate)

    # --- sample ---
    sam_p = sub.add_parser("sample", help="Generate ~1K sample records for fixtures")
    sam_p.add_argument("--output", default="pos_simulator/sample_data")
    sam_p.set_defaults(func=cmd_sample)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
