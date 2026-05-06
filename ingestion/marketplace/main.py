"""CLI for the marketplace feed simulator.

Commands
--------
generate  Run the full simulation and write NDJSON partitions.
sample    Write a small sample (3 settle-days) to stdout or a file.

Examples
--------
  py -3 -m ingestion.marketplace.main generate
  py -3 -m ingestion.marketplace.main generate --start 2023-01-01 --end 2023-01-31
  py -3 -m ingestion.marketplace.main sample --days 3
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from .config import MarketplaceConfig
from .generator import MktFeedGenerator
from .writer import write_feed


def cmd_generate(args: argparse.Namespace) -> None:
    cfg = MarketplaceConfig(
        start_date=date.fromisoformat(args.start),
        end_date=date.fromisoformat(args.end),
        avg_orders_per_store_per_day=args.volume,
        output_dir=args.output_dir,
        seed=args.seed,
    )
    gen = MktFeedGenerator(cfg)

    current = cfg.start_date
    total_orders = 0
    total_files = 0
    while current <= cfg.end_date:
        orders_by_mkt = gen.generate_day(current)
        files = write_feed(orders_by_mkt, current, cfg.output_dir)
        day_orders = sum(len(v) for v in orders_by_mkt.values())
        total_orders += day_orders
        total_files += len(files)
        current += timedelta(days=1)

    print(f"Done. {total_orders:,} orders -> {total_files:,} files in {cfg.output_dir}/")


def cmd_sample(args: argparse.Namespace) -> None:
    cfg = MarketplaceConfig(
        start_date=date(2024, 3, 15),
        end_date=date(2024, 3, 15) + timedelta(days=args.days - 1),
        avg_orders_per_store_per_day=10,
        seed=42,
    )
    gen = MktFeedGenerator(cfg)

    current = cfg.start_date
    count = 0
    while current <= cfg.end_date:
        for orders in gen.generate_day(current).values():
            for order in orders:
                print(json.dumps(order.to_dict(), ensure_ascii=False))
                count += 1
        current += timedelta(days=1)

    print(f"\n--- {count} sample orders ---", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Marketplace feed simulator")
    sub = parser.add_subparsers(dest="command", required=True)

    gen_p = sub.add_parser("generate", help="Run full simulation")
    gen_p.add_argument("--start", default="2023-01-01")
    gen_p.add_argument("--end",   default="2024-12-31")
    gen_p.add_argument("--volume", type=int, default=40,
                       help="Avg settled orders per store per day")
    gen_p.add_argument("--output-dir", default="output/mkt_feed")
    gen_p.add_argument("--seed", type=int, default=None)

    smp_p = sub.add_parser("sample", help="Print sample records to stdout")
    smp_p.add_argument("--days", type=int, default=3)

    args = parser.parse_args()
    {"generate": cmd_generate, "sample": cmd_sample}[args.command](args)


if __name__ == "__main__":
    main()
