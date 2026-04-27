"""Tests for the fault injector — one test per fault type."""

import random
from datetime import date

import pytest

from pos_simulator.config import SimConfig
from pos_simulator.faults import inject_faults
from pos_simulator.generator import TransactionGenerator


@pytest.fixture
def base_records():
    cfg = SimConfig(
        store_ids=[1],
        start_date=date(2024, 3, 15),
        end_date=date(2024, 3, 15),
        avg_trans_per_till_per_day=50,
        fault_rate=0.0,
        seed=99,
    )
    gen = TransactionGenerator(cfg)
    return gen.generate_day(1, cfg.start_date)


def _inject_one(records, fault_type, seed=0):
    """Force exactly one record of a specific fault type for testing."""
    import pos_simulator.faults as f_mod
    rng = random.Random(seed)
    # Patch fault types list to only this one
    original = f_mod._FAULT_TYPES[:]
    f_mod._FAULT_TYPES[:] = [fault_type]
    try:
        result = inject_faults(records, fault_rate=0.05, rng=rng)
    finally:
        f_mod._FAULT_TYPES[:] = original
    return result


def _faulty(records, fault_type):
    return [
        r for r in records
        if r.tran_head.__dict__.get("_fault_type") == fault_type
    ]


class TestFaultInjectionRate:

    def test_approx_fault_rate(self, base_records):
        rng = random.Random(7)
        result = inject_faults(base_records, fault_rate=0.02, rng=rng)
        faulty = [r for r in result if "_fault_type" in r.tran_head.__dict__]
        rate = len(faulty) / len(result)
        assert 0.0 <= rate <= 0.10, f"Fault rate {rate:.3f} out of expected range"

    def test_total_count_unchanged(self, base_records):
        rng = random.Random(1)
        result = inject_faults(base_records, fault_rate=0.02, rng=rng)
        assert len(result) == len(base_records)


class TestMissingTender:

    def test_missing_tender_removes_tenders(self, base_records):
        result = _inject_one(base_records, "MISSING_TENDER")
        faults = _faulty(result, "MISSING_TENDER")
        assert len(faults) > 0
        for r in faults:
            assert r.tran_tender == [], "MISSING_TENDER record should have no tenders"


class TestTenderVariance:

    def test_tender_mismatch_gt_01(self, base_records):
        result = _inject_one(base_records, "TENDER_VAR_GT_01")
        faults = _faulty(result, "TENDER_VAR_GT_01")
        for r in faults:
            if r.tran_tender:
                tender_sum = sum(t.tender_amt for t in r.tran_tender)
                diff = abs(tender_sum - r.tran_head.value)
                assert diff > 0.01, f"Expected >0.01 variance, got {diff}"


class TestTranNoDup:

    def test_duplicate_tran_no_is_set(self, base_records):
        result = _inject_one(base_records, "TRAN_NO_DUP", seed=5)
        faults = _faulty(result, "TRAN_NO_DUP")
        if faults:
            for r in faults:
                # The tran_no should appear at least twice across records for same store+register
                key = f"{r.tran_head.store}-{r.tran_head.register}"
                matching = [
                    rec for rec in result
                    if rec.tran_head.store == r.tran_head.store
                    and rec.tran_head.register == r.tran_head.register
                    and rec.tran_head.tran_no == r.tran_head.tran_no
                ]
                assert len(matching) >= 2


class TestVoidOOH:

    def test_void_ooh_has_pvoid_type(self, base_records):
        result = _inject_one(base_records, "VOID_OOH")
        faults = _faulty(result, "VOID_OOH")
        assert len(faults) > 0
        for r in faults:
            assert r.tran_head.tran_type == "PVOID"

    def test_void_ooh_time_outside_hours(self, base_records):
        result = _inject_one(base_records, "VOID_OOH")
        faults = _faulty(result, "VOID_OOH")
        for r in faults:
            hour = int(r.tran_head.tran_datetime[11:13])
            assert hour < 6 or hour >= 23, f"Expected off-hours, got hour={hour}"


class TestNegQtyNoRef:

    def test_neg_qty_on_sale(self, base_records):
        result = _inject_one(base_records, "NEG_QTY_NO_REF")
        faults = _faulty(result, "NEG_QTY_NO_REF")
        for r in faults:
            assert r.tran_head.tran_type == "SALE"
            for item in r.tran_item:
                assert item.qty < 0
                assert item.return_reason_code is None


class TestCCNotMasked:

    def test_cc_not_masked(self, base_records):
        result = _inject_one(base_records, "CC_NOT_MASKED")
        faults = _faulty(result, "CC_NOT_MASKED")
        for r in faults:
            card_tenders = [t for t in r.tran_tender if t.tender_type_group == "CARD"]
            for t in card_tenders:
                if t.cc_no:
                    assert not t.cc_no.startswith("*"), "CC should be unmasked for this fault"
