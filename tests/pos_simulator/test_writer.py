"""Tests for the NDJSON writer — output shape and partitioning."""

import json
import os
import tempfile
from datetime import date
from pathlib import Path

import pytest

from pos_simulator.config import SimConfig
from pos_simulator.generator import TransactionGenerator
from pos_simulator.writer import count_output, write_records


@pytest.fixture
def small_records():
    cfg = SimConfig(
        store_ids=[5],
        start_date=date(2024, 6, 1),
        end_date=date(2024, 6, 1),
        avg_trans_per_till_per_day=10,
        seed=7,
    )
    gen = TransactionGenerator(cfg)
    return gen.generate_day(5, cfg.start_date)


class TestWriterPartitioning:

    def test_creates_store_partition(self, small_records, tmp_path):
        write_records(small_records, tmp_path)
        store_dirs = list(tmp_path.glob("store=*"))
        assert len(store_dirs) >= 1
        assert any("store=5" in str(d) for d in store_dirs)

    def test_creates_date_partition(self, small_records, tmp_path):
        write_records(small_records, tmp_path)
        date_dirs = list(tmp_path.glob("store=*/date=*"))
        assert len(date_dirs) >= 1
        assert any("date=2024-06-01" in str(d) for d in date_dirs)

    def test_creates_hour_partition(self, small_records, tmp_path):
        write_records(small_records, tmp_path)
        hour_dirs = list(tmp_path.glob("store=*/date=*/hour=*"))
        assert len(hour_dirs) >= 1

    def test_ndjson_extension(self, small_records, tmp_path):
        write_records(small_records, tmp_path)
        ndjson_files = list(tmp_path.rglob("*.ndjson"))
        assert len(ndjson_files) >= 1


class TestWriterContent:

    def test_each_line_is_valid_json(self, small_records, tmp_path):
        write_records(small_records, tmp_path)
        for path in tmp_path.rglob("*.ndjson"):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        obj = json.loads(line)
                        assert "rtlog_orig_sys" in obj

    def test_total_count_matches(self, small_records, tmp_path):
        written = write_records(small_records, tmp_path)
        total_written = sum(written.values())
        total_counted = count_output(tmp_path)
        assert total_written == total_counted

    def test_all_records_written(self, small_records, tmp_path):
        written = write_records(small_records, tmp_path)
        assert sum(written.values()) == len(small_records)

    def test_rtlog_orig_sys_is_POS(self, small_records, tmp_path):
        write_records(small_records, tmp_path)
        for path in tmp_path.rglob("*.ndjson"):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        obj = json.loads(line)
                        assert obj["rtlog_orig_sys"] == "POS"

    def test_class_key_not_class_underscore(self, small_records, tmp_path):
        write_records(small_records, tmp_path)
        for path in tmp_path.rglob("*.ndjson"):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        obj = json.loads(line)
                        for item in obj.get("tran_item", []):
                            assert "class" in item
                            assert "class_" not in item
