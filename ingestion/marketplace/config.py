"""Configuration for the marketplace feed simulator."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class MarketplaceConfig:
    """All tunable knobs for the marketplace feed simulator.

    Volume: avg_orders_per_store_per_day controls throughput.
    Reduce this or narrow the date range to cut output size.
    """

    # Date range to simulate
    start_date: date = field(default_factory=lambda: date(2023, 1, 1))
    end_date: date = field(default_factory=lambda: date(2024, 12, 31))

    # Average settled orders per store per day (across all marketplaces)
    avg_orders_per_store_per_day: int = 40

    # ~2% bad records injected (same pattern as POS simulator)
    fault_rate: float = 0.02

    # Output base directory
    output_dir: str = "output/mkt_feed"

    # Reproducibility
    seed: int | None = None

    # Path to the store metadata CSV (None = bundled stores.csv in pos_simulator/)
    stores_file: str | None = None

    def stores(self) -> dict:
        """Return the store registry (reuses pos_simulator store_registry)."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from pos_simulator.store_registry import load_stores
        return load_stores(self.stores_file)
