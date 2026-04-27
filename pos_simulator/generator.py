"""Transaction generator — builds one complete, valid RTLOG record.

Generates all TRAN_TYPEs with realistic temporal patterns:
  - SALE       ~85% of transactions
  - RETURN     ~7%
  - PVOID      ~3%
  - PAIDIN     ~1%
  - PAIDOUT    ~1%
  - NOSALE     ~2%
  - OPEN/CLOSE ~1 per store per day each

Monetary values use Python Decimal internally, serialised as float rounded
to 4dp to match NUMERIC(20,4).
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterator

from .config import SimConfig
from .models import (
    RtlogRecord,
    TranDisc,
    TranHead,
    TranIgtax,
    TranItem,
    TranTax,
    TranTender,
)
from .reference_data import (
    ADDITIVE_TAX,
    CASHIER_IDS,
    HOUR_WEIGHTS,
    IGTAX_AUTHORITIES,
    PROMOTIONS,
    SALESPERSON_IDS,
    SKU_POOL,
    TENDER_TYPES,
)

_D = Decimal


def _round4(value: Decimal) -> float:
    return float(value.quantize(_D("0.0001"), rounding=ROUND_HALF_UP))


def _weighted_hour(rng: random.Random) -> int:
    return rng.choices(range(24), weights=HOUR_WEIGHTS, k=1)[0]


def _day_multiplier(d: date) -> float:
    """Scale transaction volume by day-of-week and special dates."""
    dow = d.weekday()  # 0=Mon, 6=Sun
    base = {0: 1.0, 1: 1.0, 2: 1.0, 3: 1.0, 4: 1.2, 5: 1.8, 6: 1.6}.get(dow, 1.0)
    # Black Friday (last Friday of November)
    if d.month == 11 and dow == 4:
        from calendar import monthrange
        _, days_in_month = monthrange(d.year, d.month)
        last_fri = max(day for day in range(1, days_in_month + 1)
                       if date(d.year, 11, day).weekday() == 4)
        if d.day == last_fri:
            base *= 4.0
    return base


class TransactionGenerator:
    """Generates RtlogRecord objects for a given store and date range."""

    def __init__(self, cfg: SimConfig) -> None:
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def generate_day(self, store: int, business_date: date) -> list[RtlogRecord]:
        """All transactions for one store on one business day."""
        records: list[RtlogRecord] = []
        multiplier = _day_multiplier(business_date)
        target = max(1, int(
            self.cfg.avg_trans_per_till_per_day * self.cfg.tills_per_store * multiplier
        ))

        tran_no_counter: dict[str, int] = {}  # register → next tran_no

        # OPEN transactions — one per till at store open (hour 6)
        for till_idx in range(1, self.cfg.tills_per_store + 1):
            register = f"TILL{till_idx:02d}"
            records.append(self._build_open_close(store, business_date, register, "OPEN", 6))
            tran_no_counter[register] = 1

        # SALE / RETURN / PVOID / PAIDIN / PAIDOUT / NOSALE
        for _ in range(target):
            tran_type = self.rng.choices(
                ["SALE", "RETURN", "PVOID", "PAIDIN", "PAIDOUT", "NOSALE"],
                weights=[85, 7, 3, 1, 1, 2],
                k=1,
            )[0]
            register = f"TILL{self.rng.randint(1, self.cfg.tills_per_store):02d}"
            tran_no = tran_no_counter[register]
            tran_no_counter[register] += 1
            hour = _weighted_hour(self.rng)
            record = self._build_transaction(
                store, business_date, register, tran_no, tran_type, hour
            )
            records.append(record)

        # CLOSE transactions — one per till at store close (hour 22)
        for till_idx in range(1, self.cfg.tills_per_store + 1):
            register = f"TILL{till_idx:02d}"
            records.append(self._build_open_close(store, business_date, register, "CLOSE", 22))

        return records

    def generate_range(self, store: int, start: date, end: date) -> Iterator[RtlogRecord]:
        current = start
        while current <= end:
            yield from self.generate_day(store, current)
            current += timedelta(days=1)

    # ------------------------------------------------------------------
    # Internal builders
    # ------------------------------------------------------------------

    def _tran_seq_no(self, store: int, register: str, d: date, tran_no: int) -> str:
        return f"STR{store:03d}-{register}-{d.isoformat()}-{tran_no:06d}"

    def _now_str(self, d: date, hour: int) -> str:
        minute = self.rng.randint(0, 59)
        second = self.rng.randint(0, 59)
        dt = datetime(d.year, d.month, d.day, hour, minute, second,
                      tzinfo=timezone(timedelta(hours=5, minutes=30)))
        return dt.isoformat()

    def _build_open_close(
        self, store: int, d: date, register: str, tran_type: str, hour: int
    ) -> RtlogRecord:
        tran_no = 9000 if tran_type == "OPEN" else 9999
        ts = self._now_str(d, hour)
        head = TranHead(
            store=store,
            business_date=d.isoformat(),
            tran_seq_no=self._tran_seq_no(store, register, d, tran_no),
            tran_datetime=ts,
            register=register,
            tran_no=tran_no,
            cashier=self.rng.choice(CASHIER_IDS),
            salesperson=self.rng.choice(SALESPERSON_IDS),
            tran_type=tran_type,
            status="IMPORTED",
            value=0.0,
            pos_tran_ind="Y",
            error_ind="N",
            banner_no=self.cfg.banner_no,
            rtlog_orig_sys="POS",
            update_datetime=ts,
            update_id="SIM",
        )
        return RtlogRecord(rtlog_orig_sys="POS", tran_head=head)

    def _build_transaction(
        self,
        store: int,
        d: date,
        register: str,
        tran_no: int,
        tran_type: str,
        hour: int,
    ) -> RtlogRecord:
        ts = self._now_str(d, hour)
        cashier = self.rng.choice(CASHIER_IDS)
        sp = self.rng.choice(SALESPERSON_IDS)

        # Non-merch transaction types carry no items/tenders in standard ReSA
        if tran_type in ("NOSALE",):
            head = TranHead(
                store=store, business_date=d.isoformat(),
                tran_seq_no=self._tran_seq_no(store, register, d, tran_no),
                tran_datetime=ts, register=register, tran_no=tran_no,
                cashier=cashier, salesperson=sp, tran_type=tran_type,
                status="IMPORTED", value=0.0, pos_tran_ind="Y",
                error_ind="N", banner_no=self.cfg.banner_no,
                rtlog_orig_sys="POS", update_datetime=ts, update_id="SIM",
            )
            return RtlogRecord(rtlog_orig_sys="POS", tran_head=head)

        if tran_type in ("PAIDIN", "PAIDOUT"):
            return self._build_paidinout(store, d, register, tran_no, tran_type, ts, cashier, sp)

        # SALE / RETURN / PVOID
        items, discs, igtax_rows, tax_rows = self._build_items(tran_type, d, hour)
        item_total = _D(str(sum(
            i.qty * i.unit_retail for i in items
        ))).quantize(_D("0.0001"), rounding=ROUND_HALF_UP)
        disc_total = _D(str(sum(
            d_.qty * d_.unit_discount_amt for d_ in discs
        ))).quantize(_D("0.0001"), rounding=ROUND_HALF_UP)
        value = (item_total - disc_total).quantize(_D("0.0001"), rounding=ROUND_HALF_UP)
        if value < 0:
            value = _D("0.0000")

        tenders = self._build_tenders(value)

        # Loyalty REF_NO1 — 30% of SALE transactions
        ref_no1 = None
        if tran_type == "SALE" and self.rng.random() < 0.30:
            ref_no1 = f"LOYALTY-{self.rng.randint(1000000, 9999999)}"

        head = TranHead(
            store=store,
            business_date=d.isoformat(),
            tran_seq_no=self._tran_seq_no(store, register, d, tran_no),
            tran_datetime=ts,
            register=register,
            tran_no=tran_no,
            cashier=cashier,
            salesperson=sp,
            tran_type=tran_type,
            status="IMPORTED",
            value=_round4(value),
            pos_tran_ind="Y",
            error_ind="N",
            banner_no=self.cfg.banner_no,
            rtlog_orig_sys="POS",
            update_datetime=ts,
            update_id="SIM",
            ref_no1=ref_no1,
        )
        return RtlogRecord(
            rtlog_orig_sys="POS",
            tran_head=head,
            tran_item=items,
            tran_disc=discs,
            tran_tender=tenders,
            tran_tax=tax_rows,
            tran_igtax=igtax_rows,
        )

    def _build_paidinout(
        self, store: int, d: date, register: str, tran_no: int,
        tran_type: str, ts: str, cashier: str, sp: str,
    ) -> RtlogRecord:
        amount = _D(str(round(self.rng.uniform(500, 5000), 2)))
        amount = amount.quantize(_D("0.0001"), rounding=ROUND_HALF_UP)
        tender = TranTender(
            tender_seq_no=1,
            tender_type_group="CASH",
            tender_type_id=1,
            tender_amt=_round4(amount),
            error_ind="N",
            orig_currency=self.cfg.currency,
            orig_curr_amt=_round4(amount),
        )
        head = TranHead(
            store=store, business_date=d.isoformat(),
            tran_seq_no=self._tran_seq_no(store, register, d, tran_no),
            tran_datetime=ts, register=register, tran_no=tran_no,
            cashier=cashier, salesperson=sp, tran_type=tran_type,
            status="IMPORTED", value=_round4(amount), pos_tran_ind="N",
            error_ind="N", banner_no=self.cfg.banner_no,
            rtlog_orig_sys="POS", update_datetime=ts, update_id="SIM",
            vendor_no=f"VND{self.rng.randint(100, 999)}",
        )
        return RtlogRecord(rtlog_orig_sys="POS", tran_head=head, tran_tender=[tender])

    def _build_items(
        self, tran_type: str, d: date, hour: int
    ) -> tuple[list[TranItem], list[TranDisc], list[TranIgtax], list[TranTax]]:
        n_items = self.rng.randint(1, 5)
        items: list[TranItem] = []
        discs: list[TranDisc] = []
        igtax_rows: list[TranIgtax] = []
        tax_rows: list[TranTax] = []
        igtax_seq = 1
        tax_seq = 1
        disc_seq = 1

        for seq in range(1, n_items + 1):
            product = self.rng.choice(SKU_POOL)
            base = _D(str(product["base_price"]))
            qty = _D(str(self.rng.randint(1, 3)))
            if tran_type == "RETURN":
                qty = -qty

            item_status = "ACTIVE" if tran_type != "PVOID" else "VOIDED"
            unit_retail = base + _D(str(self.rng.randint(-50, 50)))
            if unit_retail <= 0:
                unit_retail = base

            # Inclusive tax (IGTAX) — Indian GST baked in
            igtax_amt = _D("0.0000")
            if self.cfg.tax_mode in ("IGTAX", "BOTH"):
                for auth in IGTAX_AUTHORITIES:
                    line_igtax = (unit_retail * qty * _D(str(auth["igtax_rate"]))).quantize(
                        _D("0.0001"), rounding=ROUND_HALF_UP
                    )
                    igtax_amt += line_igtax
                    igtax_rows.append(TranIgtax(
                        item_seq_no=seq,
                        igtax_seq_no=igtax_seq,
                        tax_authority=auth["tax_authority"],
                        igtax_code=auth["igtax_code"],
                        igtax_rate=auth["igtax_rate"],
                        total_igtax_amt=_round4(line_igtax),
                        error_ind="N",
                    ))
                    igtax_seq += 1

            # Additive tax (TAX)
            if self.cfg.tax_mode in ("TAX", "BOTH"):
                tax_amt = (unit_retail * qty * _D(str(ADDITIVE_TAX["rate"]))).quantize(
                    _D("0.0001"), rounding=ROUND_HALF_UP
                )
                tax_rows.append(TranTax(
                    tax_code=ADDITIVE_TAX["tax_code"],
                    tax_seq_no=tax_seq,
                    tax_amt=_round4(tax_amt),
                    error_ind="N",
                ))
                tax_seq += 1

            items.append(TranItem(
                item_seq_no=seq,
                item_status=item_status,
                item_type="ITM",
                item=product["sku"],
                dept=product["dept"],
                class_=product["class"],
                subclass=product["subclass"],
                qty=float(qty),
                unit_retail=_round4(unit_retail),
                selling_uom="EA",
                tax_ind="Y",
                item_swiped_ind="N",
                error_ind="N",
                drop_ship_ind="N",
                unit_retail_vat_incl="Y" if self.cfg.tax_mode in ("IGTAX", "BOTH") else "N",
                total_igtax_amt=_round4(igtax_amt) if igtax_amt else None,
                uom_quantity=float(qty),
                return_reason_code="CUST" if tran_type == "RETURN" else None,
            ))

            # Discount — 25% chance per item
            if self.rng.random() < 0.25 and tran_type == "SALE":
                promo = self.rng.choice(PROMOTIONS)
                unit_disc = (unit_retail * _D(str(promo["disc_pct"]))).quantize(
                    _D("0.0001"), rounding=ROUND_HALF_UP
                )
                discs.append(TranDisc(
                    item_seq_no=seq,
                    discount_seq_no=disc_seq,
                    rms_promo_type=promo["rms_promo_type"],
                    promotion=promo["promotion"],
                    disc_type="PROMO",
                    qty=float(qty),
                    unit_discount_amt=_round4(unit_disc),
                    error_ind="N",
                    uom_quantity=float(qty),
                ))
                disc_seq += 1

        return items, discs, igtax_rows, tax_rows

    def _build_tenders(self, value: Decimal) -> list[TranTender]:
        if value <= 0:
            return []

        tender_type = self.rng.choices(
            TENDER_TYPES, weights=[t["weight"] for t in TENDER_TYPES], k=1
        )[0]
        tenders = [TranTender(
            tender_seq_no=1,
            tender_type_group=tender_type["tender_type_group"],
            tender_type_id=tender_type["tender_type_id"],
            tender_amt=_round4(value),
            error_ind="N",
            cc_no=f"************{self.rng.randint(1000, 9999)}"
                if tender_type["tender_type_group"] == "CARD" else None,
            cc_auth_no=f"A{self.rng.randint(10000, 99999)}"
                if tender_type["tender_type_group"] == "CARD" else None,
            cc_entry_mode=self.rng.choice(["CHIP", "CONTACTLESS", "SWIPE"])
                if tender_type["tender_type_group"] == "CARD" else None,
            orig_currency=self.cfg.currency,
            orig_curr_amt=_round4(value),
        )]
        return tenders
