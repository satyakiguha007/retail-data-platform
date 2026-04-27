from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .store_registry import StoreRecord


def _default_store_ids() -> list[int]:
    from .store_registry import load_stores
    return list(load_stores().keys())


@dataclass
class SimConfig:
    """All tunable knobs for the POS RTLOG simulator.

    By default, store_ids and per-store tills/currency are loaded from
    pos_simulator/stores.csv.  Pass an explicit store_ids list or a custom
    stores_file path to override.

    Volume knob: reduce avg_trans_per_till_per_day or store_ids list to cut output size.
    """

    # Store fleet — defaults to every store in stores.csv
    store_ids: list[int] = field(default_factory=_default_store_ids)

    # Path to the store metadata CSV (None = bundled stores.csv)
    stores_file: str | None = None

    # Fallback tills-per-store used when a store_id is not in the registry
    tills_per_store: int = 4

    # Date range
    start_date: date = field(default_factory=lambda: date(2023, 1, 1))
    end_date: date = field(default_factory=lambda: date(2024, 12, 31))

    # Volume (average SALE transactions per till per day — other types are derived)
    avg_trans_per_till_per_day: int = 150

    # Tax mode for item lines
    tax_mode: Literal["IGTAX", "TAX", "BOTH"] = "IGTAX"

    # ~2% bad records injected by the fault layer
    fault_rate: float = 0.02

    # Output
    output_dir: str = "output/pos_rtlog"

    # Reproducibility
    seed: int | None = None

    # Banner number (single banner for v1)
    banner_no: int = 1

    # Fallback currency used when a store_id is not in the registry
    currency: str = "INR"

    def stores(self) -> dict[int, StoreRecord]:
        """Return the store registry for this config."""
        from .store_registry import load_stores
        return load_stores(self.stores_file)
