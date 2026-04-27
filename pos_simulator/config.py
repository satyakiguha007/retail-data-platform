from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal


@dataclass
class SimConfig:
    """All tunable knobs for the POS RTLOG simulator.

    Volume knob: reduce avg_trans_per_till_per_day or store_ids list to cut output size.
    """

    # Store fleet
    store_ids: list[int] = field(default_factory=lambda: list(range(1, 51)))
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

    # Currency (INR for Indian retail scenario)
    currency: str = "INR"
