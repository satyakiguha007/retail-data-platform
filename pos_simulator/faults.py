"""Fault injector — wraps a stream of valid RtlogRecords and corrupts ~fault_rate of them.

Each corrupted record maps to one of the 18 Sales Audit rules so the downstream
audit engine has something real to catch.

Fault types implemented (6 of 18 audit rules):
  MISSING_TENDER    — tran_head has no SA_TRAN_TENDER rows
  TENDER_VAR_GT_01  — tender total differs from tran_head.VALUE by >0.01
  TRAN_NO_DUP       — duplicate tran_no reused on same store/register
  VOID_OOH          — PVOID outside 06:00-23:00
  NEG_QTY_NO_REF    — negative qty on a SALE (not RETURN)
  CC_NOT_MASKED     — card tender with un-masked CC number
"""

from __future__ import annotations

import copy
import random
from typing import Iterator

from .models import RtlogRecord, TranTender


_FAULT_TYPES = [
    "MISSING_TENDER",
    "TENDER_VAR_GT_01",
    "TRAN_NO_DUP",
    "VOID_OOH",
    "NEG_QTY_NO_REF",
    "CC_NOT_MASKED",
]


def inject_faults(
    records: list[RtlogRecord],
    fault_rate: float,
    rng: random.Random,
) -> list[RtlogRecord]:
    """Return a new list with ~fault_rate records corrupted.

    Faults are injected in-place on deep copies so the originals are untouched.
    A _fault_type attribute is attached to the tran_head for test introspection.
    """
    result: list[RtlogRecord] = []
    seen_tran_nos: dict[str, set[int]] = {}  # "store-register" → set of tran_nos

    for record in records:
        key = f"{record.tran_head.store}-{record.tran_head.register}"
        seen_tran_nos.setdefault(key, set())
        seen_tran_nos[key].add(record.tran_head.tran_no)

    # Determine which records get a fault
    n_faults = max(0, round(len(records) * fault_rate))
    fault_indices = set(rng.sample(range(len(records)), min(n_faults, len(records))))
    fault_cycle = _FAULT_TYPES.copy()
    rng.shuffle(fault_cycle)
    fault_assignment: list[str] = []
    while len(fault_assignment) < len(fault_indices):
        fault_assignment.extend(fault_cycle)
    fault_assignment = fault_assignment[: len(fault_indices)]

    fi_iter = iter(sorted(fault_indices))
    fa_iter = iter(fault_assignment)

    for idx, record in enumerate(records):
        if idx in fault_indices:
            fault_type = next(fa_iter)
            corrupted = _corrupt(record, fault_type, rng, seen_tran_nos)
            result.append(corrupted)
        else:
            result.append(record)

    return result


def _corrupt(
    record: RtlogRecord,
    fault_type: str,
    rng: random.Random,
    seen_tran_nos: dict[str, set[int]],
) -> RtlogRecord:
    r = copy.deepcopy(record)
    r.tran_head.__dict__["_fault_type"] = fault_type  # test hook

    if fault_type == "MISSING_TENDER":
        r.tran_tender = []

    elif fault_type == "TENDER_VAR_GT_01":
        if r.tran_tender:
            delta = round(rng.uniform(0.02, 5.00), 4)
            r.tran_tender[0].tender_amt = round(
                r.tran_tender[0].tender_amt + delta, 4
            )

    elif fault_type == "TRAN_NO_DUP":
        key = f"{r.tran_head.store}-{r.tran_head.register}"
        candidates = [tn for tn in seen_tran_nos.get(key, set())
                      if tn != r.tran_head.tran_no]
        if candidates:
            dup_no = rng.choice(candidates)
            r.tran_head.tran_no = dup_no
            old_seq = r.tran_head.tran_seq_no
            r.tran_head.tran_seq_no = old_seq.rsplit("-", 1)[0] + f"-{dup_no:06d}"

    elif fault_type == "VOID_OOH":
        r.tran_head.tran_type = "PVOID"
        # Force time to 02:00 (outside 06:00-23:00)
        ts = r.tran_head.tran_datetime
        r.tran_head.tran_datetime = ts[:11] + "02:00:00" + ts[19:]

    elif fault_type == "NEG_QTY_NO_REF":
        r.tran_head.tran_type = "SALE"
        for item in r.tran_item:
            item.qty = -abs(item.qty)      # force negative regardless of original sign
            item.uom_quantity = item.qty
            item.return_reason_code = None  # no return reason on a SALE — makes it invalid

    elif fault_type == "CC_NOT_MASKED":
        for tender in r.tran_tender:
            if tender.tender_type_group == "CARD":
                # Store a full (fake) CC number — violates PCI masking rule
                tender.cc_no = f"{rng.randint(4000000000000000, 4999999999999999)}"

    return r
