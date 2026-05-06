"""Dataclasses for marketplace feed records."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MktOrderItem:
    line_no: int
    sku: str
    dept: str
    class_: str
    subclass: str
    qty: int
    unit_price: float
    discount_amt: float
    total_amt: float

    def to_dict(self) -> dict:
        return {
            "line_no":      self.line_no,
            "sku":          self.sku,
            "dept":         self.dept,
            "class":        self.class_,   # rename: class_ → class in JSON
            "subclass":     self.subclass,
            "qty":          self.qty,
            "unit_price":   self.unit_price,
            "discount_amt": self.discount_amt,
            "total_amt":    self.total_amt,
        }


@dataclass
class MktOrder:
    order_id: str
    marketplace: str
    store_no: int
    order_date: str
    settle_date: str
    currency: str
    customer_id: str
    status: str
    items: list[MktOrderItem]
    subtotal_amt: float
    discount_amt: float
    total_amt: float
    commission_rate: float
    commission_amt: float
    rtlog_orig_sys: str = "MKT"

    def to_dict(self) -> dict:
        return {
            "rtlog_orig_sys":  self.rtlog_orig_sys,
            "order_id":        self.order_id,
            "marketplace":     self.marketplace,
            "store_no":        self.store_no,
            "order_date":      self.order_date,
            "settle_date":     self.settle_date,
            "currency":        self.currency,
            "customer_id":     self.customer_id,
            "status":          self.status,
            "items":           [i.to_dict() for i in self.items],
            "subtotal_amt":    self.subtotal_amt,
            "discount_amt":    self.discount_amt,
            "total_amt":       self.total_amt,
            "commission_rate": self.commission_rate,
            "commission_amt":  self.commission_amt,
        }
