"""RTLOG record dataclasses.

Field names mirror Oracle ReSA column names (lowercase).
JSON serialisation via dataclasses.asdict() — None values are preserved
so the downstream conformance job can distinguish missing from absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Child records
# ---------------------------------------------------------------------------

@dataclass
class TranItem:
    item_seq_no: int
    item_status: str                  # ACTIVE / VOIDED / REFUNDED
    item_type: str                    # ITM / GCN / REF / TND
    item: Optional[str]               # SKU
    dept: Optional[int]
    class_: Optional[int]             # 'class' is a Python keyword — serialised as 'class'
    subclass: Optional[int]
    qty: float
    unit_retail: float                # NUMERIC(20,4) — DECIMAL in Spark
    selling_uom: str
    tax_ind: str                      # Y/N
    item_swiped_ind: str              # Y/N
    error_ind: str                    # Y/N
    drop_ship_ind: str                # Y/N  marketplace flag
    unit_retail_vat_incl: str         # Y/N  default N
    total_igtax_amt: Optional[float]
    uom_quantity: float
    return_reason_code: Optional[str] = None
    override_reason: Optional[str] = None
    orig_unit_retail: Optional[float] = None
    cust_order_no: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "item_seq_no": self.item_seq_no,
            "item_status": self.item_status,
            "item_type": self.item_type,
            "item": self.item,
            "dept": self.dept,
            "class": self.class_,       # rename for JSON output
            "subclass": self.subclass,
            "qty": self.qty,
            "unit_retail": self.unit_retail,
            "selling_uom": self.selling_uom,
            "tax_ind": self.tax_ind,
            "item_swiped_ind": self.item_swiped_ind,
            "error_ind": self.error_ind,
            "drop_ship_ind": self.drop_ship_ind,
            "unit_retail_vat_incl": self.unit_retail_vat_incl,
            "total_igtax_amt": self.total_igtax_amt,
            "uom_quantity": self.uom_quantity,
            "return_reason_code": self.return_reason_code,
            "override_reason": self.override_reason,
            "orig_unit_retail": self.orig_unit_retail,
            "cust_order_no": self.cust_order_no,
        }
        return d


@dataclass
class TranDisc:
    item_seq_no: int
    discount_seq_no: int
    rms_promo_type: str
    promotion: Optional[int]
    disc_type: str                    # PROMO / MANUAL / COUPON / EMP
    qty: float
    unit_discount_amt: float          # NUMERIC(20,4)
    error_ind: str                    # Y/N
    uom_quantity: float
    coupon_no: Optional[str] = None
    promo_comp: Optional[int] = None


@dataclass
class TranTender:
    tender_seq_no: int
    tender_type_group: str            # CASH / CARD / CHECK / VOUCHER
    tender_type_id: int
    tender_amt: float                 # NUMERIC(20,4)
    error_ind: str                    # Y/N
    cc_no: Optional[str] = None       # masked last-4 only
    cc_auth_no: Optional[str] = None
    cc_entry_mode: Optional[str] = None   # CHIP / SWIPE / CONTACTLESS / MANUAL
    orig_currency: Optional[str] = None
    orig_curr_amt: Optional[float] = None
    voucher_no: Optional[str] = None


@dataclass
class TranTax:
    """Additive (sales-tax-style) tax record."""
    tax_code: str
    tax_seq_no: int
    tax_amt: float                    # NUMERIC(20,4)
    error_ind: str                    # Y/N


@dataclass
class TranIgtax:
    """Inclusive (VAT-style) tax record — one row per item × tax authority."""
    item_seq_no: int
    igtax_seq_no: int
    tax_authority: str
    igtax_code: str
    igtax_rate: float
    total_igtax_amt: float            # NUMERIC(20,4)
    error_ind: str                    # Y/N


# ---------------------------------------------------------------------------
# Transaction header
# ---------------------------------------------------------------------------

@dataclass
class TranHead:
    store: int
    business_date: str                # ISO date string
    tran_seq_no: str                  # composite: STR{store}-{register}-{date}-{seq}
    tran_datetime: str                # ISO datetime with offset
    register: str
    tran_no: int
    cashier: str
    salesperson: str
    tran_type: str                    # SALE / RETURN / PVOID / PAIDIN / PAIDOUT / NOSALE / OPEN / CLOSE
    status: str                       # IMPORTED
    value: float                      # NUMERIC(20,4)  grand total
    pos_tran_ind: str                 # Y/N
    error_ind: str                    # Y/N
    banner_no: int
    rtlog_orig_sys: str               # POS
    update_datetime: str
    update_id: str
    rev_no: int = 0
    sub_tran_type: Optional[str] = None
    reason_code: Optional[str] = None
    orig_tran_no: Optional[int] = None
    orig_tran_type: Optional[str] = None
    orig_reg_no: Optional[str] = None
    vendor_no: Optional[str] = None
    vendor_invc_no: Optional[str] = None
    rounded_amt: Optional[float] = None
    rounded_off_amt: Optional[float] = None
    ref_no1: Optional[str] = None     # loyalty ID
    ref_no2: Optional[str] = None


# ---------------------------------------------------------------------------
# Top-level RTLOG record
# ---------------------------------------------------------------------------

@dataclass
class RtlogRecord:
    rtlog_orig_sys: str               # always 'POS' for Module 1
    tran_head: TranHead
    tran_item: list[TranItem] = field(default_factory=list)
    tran_disc: list[TranDisc] = field(default_factory=list)
    tran_tender: list[TranTender] = field(default_factory=list)
    tran_tax: list[TranTax] = field(default_factory=list)
    tran_igtax: list[TranIgtax] = field(default_factory=list)

    def to_dict(self) -> dict:
        import dataclasses

        def _asdict(obj):
            if isinstance(obj, TranItem):
                return obj.to_dict()
            if dataclasses.is_dataclass(obj):
                return {k: _asdict(v) for k, v in dataclasses.asdict(obj).items()}
            if isinstance(obj, list):
                return [_asdict(i) for i in obj]
            return obj

        return {
            "rtlog_orig_sys": self.rtlog_orig_sys,
            "tran_head": _asdict(self.tran_head),
            "tran_item": [item.to_dict() for item in self.tran_item],
            "tran_disc": [_asdict(d) for d in self.tran_disc],
            "tran_tender": [_asdict(t) for t in self.tran_tender],
            "tran_tax": [_asdict(t) for t in self.tran_tax],
            "tran_igtax": [_asdict(t) for t in self.tran_igtax],
        }
