"""Capital-ledger tests for the backtest engine."""

import math

from agent.backtest import _entry_share_count, _portfolio_nav
from agent.strategy import FEE_RATE


def test_portfolio_nav_is_cash_plus_marked_positions():
    positions = {
        "2330": {"shares": 100, "entry_price": 500.0, "last_mark": 510.0},
        "2317": {"shares": 200, "entry_price": 100.0, "last_mark": 105.0},
    }
    nav = _portfolio_nav(25_000.0, positions, {"2330": 520.0, "2317": 110.0})
    assert nav == 99_000.0


def test_portfolio_nav_falls_back_to_last_mark():
    positions = {
        "2330": {"shares": 100, "entry_price": 500.0, "last_mark": 510.0},
    }
    assert _portfolio_nav(10_000.0, positions, {"2330": math.nan}) == 61_000.0


def test_entry_size_compounds_with_current_nav():
    price = 100.0
    initial = _entry_share_count(price, 1_000_000, 1_000_000, 10, None, 0.01)
    after_gain = _entry_share_count(price, 1_500_000, 1_500_000, 10, None, 0.01)
    after_loss = _entry_share_count(price, 700_000, 700_000, 10, None, 0.01)

    assert after_gain > initial > after_loss
    assert initial == int((1_000_000 / 10) // (price * (1 + FEE_RATE)))


def test_entry_size_never_spends_more_cash_than_available():
    shares = _entry_share_count(
        fill=100.0,
        cash=12_000.0,
        nav=1_000_000.0,
        max_open=10,
        avg_volume=None,
        max_pct_of_avg_volume=0.01,
    )
    assert shares * 100.0 * (1 + FEE_RATE) <= 12_000.0


def test_entry_size_respects_liquidity_cap():
    shares = _entry_share_count(
        fill=100.0,
        cash=1_000_000.0,
        nav=1_000_000.0,
        max_open=10,
        avg_volume=5_000.0,
        max_pct_of_avg_volume=0.01,
    )
    assert shares == 50
