"""Marketplace feed generator.

Produces MktOrder objects for a given settlement date.  Each settled order
represents a fulfilled (DELIVERED) or refunded (RETURNED / CANCELLED_REFUND)
marketplace transaction that the retailer's OMS has finalised.

Settlement date vs order date:
  settle_date = the date appearing in the feed filename (when the batch is cut)
  order_date  = settle_date − settlement_lag  (order was placed earlier)
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .config import MarketplaceConfig
from .models import MktOrder, MktOrderItem
from .reference_data import (
    MARKETPLACES,
    ORDER_STATUS_WEIGHTS,
    PROMOTIONS,
    SETTLEMENT_LAG_WEIGHTS,
    SKU_POOL,
)

_D = Decimal


def _round2(v: Decimal) -> float:
    return float(v.quantize(_D("0.01"), rounding=ROUND_HALF_UP))


def _weighted_choice(rng: random.Random, weight_map: dict) -> object:
    keys = list(weight_map.keys())
    weights = list(weight_map.values())
    return rng.choices(keys, weights=weights, k=1)[0]


class MktFeedGenerator:
    """Generates settled marketplace orders for one day at a time."""

    def __init__(self, cfg: MarketplaceConfig) -> None:
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self._registry = cfg.stores()

        # Pre-build: marketplace → list of store_nos it services
        self._mkt_stores: dict[str, list[int]] = {}
        for mkt in MARKETPLACES:
            served = [
                s.store_no for s in self._registry.values()
                if s.country in mkt["store_countries"]
            ]
            if served:
                self._mkt_stores[mkt["marketplace"]] = served

        # Index marketplaces by name for quick lookup
        self._mkt_meta: dict[str, dict] = {m["marketplace"]: m for m in MARKETPLACES}

        self._order_seq: dict[str, int] = {}  # marketplace → running counter

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def generate_day(self, settle_date: date) -> dict[str, list[MktOrder]]:
        """Return a dict of marketplace → orders for that settle_date.

        Only marketplaces that have at least one served store are included.
        """
        result: dict[str, list[MktOrder]] = {}
        n_mkt = len(self._mkt_stores)
        if n_mkt == 0:
            return result

        orders_per_mkt_per_store = max(
            1, self.cfg.avg_orders_per_store_per_day // n_mkt
        )

        for mkt_name, store_nos in self._mkt_stores.items():
            mkt = self._mkt_meta[mkt_name]
            orders: list[MktOrder] = []
            for store_no in store_nos:
                n = max(1, int(orders_per_mkt_per_store * self._day_multiplier(settle_date)))
                for _ in range(n):
                    orders.append(self._build_order(mkt, store_no, settle_date))
            result[mkt_name] = orders

        return result

    def generate_range(self, start: date, end: date) -> dict[str, list[MktOrder]]:
        """Accumulate all orders across a date range, keyed by marketplace."""
        combined: dict[str, list[MktOrder]] = {}
        current = start
        while current <= end:
            for mkt, orders in self.generate_day(current).items():
                combined.setdefault(mkt, []).extend(orders)
            current += timedelta(days=1)
        return combined

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _day_multiplier(self, d: date) -> float:
        dow = d.weekday()
        base = {0: 1.0, 1: 1.0, 2: 1.0, 3: 1.0, 4: 1.3, 5: 1.6, 6: 1.5}.get(dow, 1.0)
        # Online shopping spikes on 11/11 (Singles Day) and Black Friday
        if d.month == 11 and d.day == 11:
            base *= 5.0
        elif d.month == 11 and d.weekday() == 4:
            from calendar import monthrange
            _, days = monthrange(d.year, 11)
            last_fri = max(day for day in range(1, days + 1)
                           if date(d.year, 11, day).weekday() == 4)
            if d.day == last_fri:
                base *= 3.5
        return base

    def _build_order(self, mkt: dict, store_no: int, settle_date: date) -> MktOrder:
        lag = _weighted_choice(self.rng, SETTLEMENT_LAG_WEIGHTS)
        order_date = settle_date - timedelta(days=lag)

        status = _weighted_choice(self.rng, ORDER_STATUS_WEIGHTS)
        currency = mkt["currency"]
        commission_rate = mkt["commission_rate"]

        seq = self._order_seq.get(mkt["marketplace"], 0) + 1
        self._order_seq[mkt["marketplace"]] = seq
        order_id = f"{mkt['marketplace']}-{order_date.strftime('%Y%m%d')}-{seq:06d}"
        customer_id = f"CUST-{self.rng.randint(1_000_000, 9_999_999)}"

        items, subtotal, discount = self._build_items(status)

        total = subtotal - discount
        if total < _D("0"):
            total = _D("0")
        commission = (total * _D(str(commission_rate))).quantize(
            _D("0.01"), rounding=ROUND_HALF_UP
        )

        return MktOrder(
            order_id=order_id,
            marketplace=mkt["marketplace"],
            store_no=store_no,
            order_date=order_date.isoformat(),
            settle_date=settle_date.isoformat(),
            currency=currency,
            customer_id=customer_id,
            status=status,
            items=items,
            subtotal_amt=_round2(subtotal),
            discount_amt=_round2(discount),
            total_amt=_round2(total),
            commission_rate=commission_rate,
            commission_amt=_round2(commission),
        )

    def _build_items(
        self, status: str
    ) -> tuple[list[MktOrderItem], Decimal, Decimal]:
        n = self.rng.randint(1, 3)
        items: list[MktOrderItem] = []
        subtotal = _D("0")
        total_disc = _D("0")

        for line_no in range(1, n + 1):
            product = self.rng.choice(SKU_POOL)
            base = _D(str(product["base_price"]))
            # ±10% price variation
            unit_price = base * _D(str(1 + self.rng.uniform(-0.10, 0.10)))
            unit_price = unit_price.quantize(_D("0.01"), rounding=ROUND_HALF_UP)
            qty = self.rng.randint(1, 2)

            # 40% chance of a promo discount on this item
            disc = _D("0")
            if self.rng.random() < 0.40:
                promo = self.rng.choice(PROMOTIONS)
                disc = (unit_price * _D(str(promo["disc_pct"]))).quantize(
                    _D("0.01"), rounding=ROUND_HALF_UP
                )

            line_total = (unit_price - disc) * qty
            subtotal += unit_price * qty
            total_disc += disc * qty

            # RETURNED orders: negate quantities
            effective_qty = -qty if status in ("RETURNED", "CANCELLED_REFUND") else qty

            items.append(MktOrderItem(
                line_no=line_no,
                sku=product["sku"],
                dept=product["dept"],
                class_=product["class"],
                subclass=product["subclass"],
                qty=effective_qty,
                unit_price=_round2(unit_price),
                discount_amt=_round2(disc),
                total_amt=_round2(line_total) if status == "DELIVERED"
                          else _round2(-line_total),
            ))

        return items, subtotal, total_disc
