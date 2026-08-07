import datetime as dt

import budget
import config
import db


def _at(day, hour=12):
    return dt.datetime(2026, 8, day, hour).timestamp()


def _fresh(tmp_path, cap=3):
    db.close()
    db.init_db(str(tmp_path / "b.db"))
    config.OUTBOUND_DAILY_BUDGET = cap


def test_starts_with_the_full_budget(tmp_path):
    _fresh(tmp_path)
    assert budget.remaining(_at(8)) == 3
    assert budget.can_spend(_at(8))


def test_spending_reduces_remaining(tmp_path):
    _fresh(tmp_path)
    budget.spend(_at(8))
    assert budget.remaining(_at(8)) == 2


def test_exhausted_budget_blocks(tmp_path):
    _fresh(tmp_path)
    for _ in range(3):
        budget.spend(_at(8))
    assert budget.remaining(_at(8)) == 0
    assert not budget.can_spend(_at(8))


def test_budget_resets_on_a_new_day(tmp_path):
    _fresh(tmp_path)
    for _ in range(3):
        budget.spend(_at(8))
    assert not budget.can_spend(_at(8))
    assert budget.can_spend(_at(9))
    assert budget.remaining(_at(9)) == 3


def test_zero_budget_means_unlimited(tmp_path):
    _fresh(tmp_path, cap=0)
    for _ in range(50):
        budget.spend(_at(8))
    assert budget.can_spend(_at(8))
