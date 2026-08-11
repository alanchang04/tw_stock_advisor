from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from scripts.run_verified_backtest import _frame_sha256, _nav_sha256, _yearly


def test_frame_hash_is_deterministic_and_order_sensitive():
    frame = pd.DataFrame([
        {"stock_id": "A", "shares": 10},
        {"stock_id": "B", "shares": 20},
    ])
    assert _frame_sha256(frame) == _frame_sha256(frame.copy())
    assert _frame_sha256(frame) != _frame_sha256(frame.iloc[::-1].reset_index(drop=True))


def test_nav_hash_sorts_dates():
    a = {dt.date(2020, 1, 2): 101.0, dt.date(2020, 1, 1): 100.0}
    b = {dt.date(2020, 1, 1): 100.0, dt.date(2020, 1, 2): 101.0}
    assert _nav_sha256(a) == _nav_sha256(b)


def test_yearly_return_chains_from_initial_nav():
    nav = {
        dt.date(2020, 1, 1): 100.0,
        dt.date(2020, 12, 31): 110.0,
        dt.date(2021, 12, 31): 99.0,
    }
    result = _yearly(nav)
    assert result["2020"] == pytest.approx(0.1)
    assert result["2021"] == pytest.approx(-0.1)
