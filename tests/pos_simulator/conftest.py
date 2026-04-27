import pytest
from datetime import date

from pos_simulator.config import SimConfig
from pos_simulator.generator import TransactionGenerator


@pytest.fixture
def cfg():
    return SimConfig(
        store_ids=[1, 2],
        start_date=date(2024, 3, 15),
        end_date=date(2024, 3, 15),
        avg_trans_per_till_per_day=20,
        fault_rate=0.0,   # clean records by default
        seed=42,
    )


@pytest.fixture
def gen(cfg):
    return TransactionGenerator(cfg)


@pytest.fixture
def one_day_records(gen, cfg):
    return gen.generate_day(1, cfg.start_date)
