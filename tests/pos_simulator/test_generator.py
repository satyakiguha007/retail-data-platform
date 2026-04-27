"""Tests for the transaction generator — valid records only (fault_rate=0)."""

from datetime import date
from decimal import Decimal

import pytest

from pos_simulator.config import SimConfig
from pos_simulator.generator import TransactionGenerator
from pos_simulator.models import RtlogRecord


def _d4(v: float) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.0001"))


class TestRtlogStructure:

    def test_rtlog_orig_sys_is_POS(self, one_day_records):
        for r in one_day_records:
            assert r.rtlog_orig_sys == "POS"
            assert r.tran_head.rtlog_orig_sys == "POS"

    def test_store_matches(self, one_day_records):
        for r in one_day_records:
            assert r.tran_head.store == 1

    def test_business_date_matches(self, one_day_records, cfg):
        for r in one_day_records:
            assert r.tran_head.business_date == cfg.start_date.isoformat()

    def test_has_open_and_close(self, one_day_records, cfg):
        types = [r.tran_head.tran_type for r in one_day_records]
        assert "OPEN" in types
        assert "CLOSE" in types

    def test_tran_seq_no_unique(self, one_day_records):
        seq_nos = [r.tran_head.tran_seq_no for r in one_day_records]
        assert len(seq_nos) == len(set(seq_nos)), "tran_seq_no values must be unique"

    def test_status_is_IMPORTED(self, one_day_records):
        for r in one_day_records:
            assert r.tran_head.status == "IMPORTED"

    def test_error_ind_is_YN(self, one_day_records):
        for r in one_day_records:
            assert r.tran_head.error_ind in ("Y", "N")

    def test_pos_tran_ind_is_YN(self, one_day_records):
        for r in one_day_records:
            assert r.tran_head.pos_tran_ind in ("Y", "N")

    def test_banner_no_matches_config(self, one_day_records, cfg):
        for r in one_day_records:
            assert r.tran_head.banner_no == cfg.banner_no


class TestSaleTransactions:

    def _sales(self, records):
        return [r for r in records if r.tran_head.tran_type == "SALE"]

    def test_sale_has_items(self, one_day_records):
        for r in self._sales(one_day_records):
            assert len(r.tran_item) >= 1

    def test_sale_has_tender(self, one_day_records):
        for r in self._sales(one_day_records):
            assert len(r.tran_tender) >= 1

    def test_tender_total_matches_value(self, one_day_records):
        for r in self._sales(one_day_records):
            tender_sum = sum(_d4(t.tender_amt) for t in r.tran_tender)
            value = _d4(r.tran_head.value)
            assert abs(tender_sum - value) <= Decimal("0.01"), (
                f"Tender mismatch: tender={tender_sum} value={value} "
                f"tran={r.tran_head.tran_seq_no}"
            )

    def test_item_seq_nos_sequential(self, one_day_records):
        for r in self._sales(one_day_records):
            seqs = [i.item_seq_no for i in r.tran_item]
            assert seqs == list(range(1, len(seqs) + 1))

    def test_unit_retail_positive(self, one_day_records):
        for r in self._sales(one_day_records):
            for item in r.tran_item:
                assert item.unit_retail > 0

    def test_item_ind_flags_are_YN(self, one_day_records):
        for r in self._sales(one_day_records):
            for item in r.tran_item:
                assert item.tax_ind in ("Y", "N")
                assert item.drop_ship_ind in ("Y", "N")
                assert item.error_ind in ("Y", "N")
                assert item.unit_retail_vat_incl in ("Y", "N")

    def test_monetary_columns_4dp(self, one_day_records):
        """value, tender_amt, unit_retail must have at most 4 decimal places."""
        for r in self._sales(one_day_records):
            assert _d4(r.tran_head.value) == Decimal(str(r.tran_head.value))
            for t in r.tran_tender:
                assert _d4(t.tender_amt) == Decimal(str(t.tender_amt))
            for i in r.tran_item:
                assert _d4(i.unit_retail) == Decimal(str(i.unit_retail))

    def test_cc_numbers_masked(self, one_day_records):
        for r in self._sales(one_day_records):
            for tender in r.tran_tender:
                if tender.tender_type_group == "CARD" and tender.cc_no:
                    assert tender.cc_no.startswith("*"), (
                        f"CC not masked: {tender.cc_no}"
                    )

    def test_drop_ship_ind_N_for_pos(self, one_day_records):
        for r in self._sales(one_day_records):
            for item in r.tran_item:
                assert item.drop_ship_ind == "N", "POS items must not be DROP_SHIP"


class TestIgtax:

    def test_igtax_present_in_igtax_mode(self):
        cfg = SimConfig(store_ids=[1], start_date=__import__("datetime").date(2024, 1, 1),
                        end_date=__import__("datetime").date(2024, 1, 1),
                        avg_trans_per_till_per_day=10, seed=1, tax_mode="IGTAX")
        gen = TransactionGenerator(cfg)
        records = gen.generate_day(1, cfg.start_date)
        sales = [r for r in records if r.tran_head.tran_type == "SALE"]
        assert any(len(r.tran_igtax) > 0 for r in sales)
        assert all(len(r.tran_tax) == 0 for r in sales)

    def test_tax_present_in_tax_mode(self):
        cfg = SimConfig(store_ids=[1], start_date=__import__("datetime").date(2024, 1, 1),
                        end_date=__import__("datetime").date(2024, 1, 1),
                        avg_trans_per_till_per_day=10, seed=1, tax_mode="TAX")
        gen = TransactionGenerator(cfg)
        records = gen.generate_day(1, cfg.start_date)
        sales = [r for r in records if r.tran_head.tran_type == "SALE"]
        assert any(len(r.tran_tax) > 0 for r in sales)
        assert all(len(r.tran_igtax) == 0 for r in sales)


class TestJsonSerialisation:

    def test_to_dict_has_required_keys(self, one_day_records):
        for r in one_day_records:
            d = r.to_dict()
            assert "rtlog_orig_sys" in d
            assert "tran_head" in d
            assert "tran_item" in d
            assert "tran_tender" in d
            assert "tran_disc" in d
            assert "tran_tax" in d
            assert "tran_igtax" in d

    def test_tran_item_uses_class_not_class_(self, one_day_records):
        """JSON key must be 'class', not 'class_' (Python keyword workaround)."""
        sales = [r for r in one_day_records if r.tran_head.tran_type == "SALE"]
        for r in sales:
            for item_dict in r.to_dict()["tran_item"]:
                assert "class" in item_dict
                assert "class_" not in item_dict

    def test_roundtrip_json(self, one_day_records):
        import json
        for r in one_day_records[:5]:
            s = json.dumps(r.to_dict())
            reloaded = json.loads(s)
            assert reloaded["tran_head"]["rtlog_orig_sys"] == "POS"
