"""F0 tests for MOM-1 PIT sector, sizing, and T+1 execution mechanics."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.momentum_execution import (
    build_equal_weight_rebalance_orders,
    equal_weight_target_shares,
    evaluate_execution_attempt,
    pit_industry_map,
    select_holdings_with_industry_cap,
)


def test_pit_industry_map_uses_latest_nonfuture_snapshot():
    frame = pd.DataFrame({
        "snapshot_date": ["2006-01-31", "2006-02-28"],
        "stock_id": ["1101", "1102"],
        "industry_code_asof": ["01", "02"],
        "industry_is_point_in_time": [True, True],
        "industry_observation_date": ["2006-01-31", "2006-02-28"],
    })
    result = pit_industry_map(frame, "2006-02-15", ["1101", "1102"])
    assert result["1101"] == "01"
    assert pd.isna(result["1102"])


def test_pit_industry_map_rejects_future_observation():
    frame = pd.DataFrame({
        "snapshot_date": ["2006-01-31"],
        "stock_id": ["1101"],
        "industry_code_asof": ["01"],
        "industry_is_point_in_time": [True],
        "industry_observation_date": ["2006-02-01"],
    })
    with pytest.raises(ValueError, match="前視"):
        pit_industry_map(frame, "2006-01-31", ["1101"])


def test_industry_cap_retains_existing_first_and_refills_from_entrants():
    signal = pd.Series({f"{1000 + i:04d}": float(i) for i in range(100)})
    # 1099..1090 are entry candidates.  Existing 1085 is top-20% and gets priority.
    industry = pd.Series("B", index=signal.index)
    industry.loc[["1085", "1099", "1098", "1097", "1096"]] = "A"
    selected = select_holdings_with_industry_cap(
        signal, industry, previous_holdings=["1085"], max_positions=5, max_per_industry=3
    )
    assert selected[0] == "1085"
    assert selected == ["1085", "1099", "1098", "1095", "1094"]


def test_industry_cap_fails_closed_on_missing_pit_industry():
    signal = pd.Series({f"{1000 + i}": float(i) for i in range(10)})
    industry = pd.Series("A", index=signal.index)
    industry.loc["1009"] = pd.NA
    assert select_holdings_with_industry_cap(signal, industry) == []
    with pytest.raises(ValueError, match="阻擋正式選股"):
        select_holdings_with_industry_cap(
            signal, industry, require_complete_industry=True
        )


def test_equal_weight_sizing_applies_notional_and_volume_caps():
    assert equal_weight_target_shares(
        executable_price=100.0, nav=300_000.0, average_volume_shares=1_000_000.0
    ) == 300
    assert equal_weight_target_shares(
        executable_price=100.0, nav=300_000.0, average_volume_shares=10_000.0
    ) == 100


def test_rebalance_orders_are_sell_first_and_use_explicit_broker_units():
    orders = build_equal_weight_rebalance_orders(
        target_holdings=["1102", "1103"],
        current_shares={"1101": 2_345, "1102": 50},
        executable_prices=pd.Series({"1101": 20.0, "1102": 100.0, "1103": 10.0}),
        average_volumes_shares=pd.Series({"1102": 1_000_000.0, "1103": 1_000_000.0}),
        nav=300_000.0,
    )
    assert [(order.side, order.stock_id) for order in orders] == [
        ("sell", "1101"), ("buy", "1102"), ("buy", "1103")
    ]
    assert orders[0].common_lots == 2
    assert orders[0].odd_lot_shares == 345
    assert orders[1].shares == 250  # target 300 less current 50
    assert orders[2].target_shares == 3_000


def test_execution_rejects_same_day_attempt():
    sessions = pd.DatetimeIndex(["2006-01-30", "2006-01-31", "2006-02-01"])
    with pytest.raises(ValueError, match="之後"):
        evaluate_execution_attempt(
            side="buy", decision_date="2006-01-31", attempt_date="2006-01-31",
            trading_days=sessions, open_price=10.0,
        )


def test_missing_tplus1_open_defers_to_next_actual_session():
    sessions = pd.DatetimeIndex(["2006-01-31", "2006-02-03", "2006-02-06"])
    result = evaluate_execution_attempt(
        side="buy", decision_date="2006-01-31", attempt_date="2006-02-03",
        trading_days=sessions, open_price=np.nan,
    )
    assert result.status == "deferred"
    assert result.deferred_to == "2006-02-06"
    assert result.reason == "missing_open"


def test_locked_limit_is_side_specific():
    sessions = pd.DatetimeIndex(["2006-01-31", "2006-02-01", "2006-02-02"])
    buy = evaluate_execution_attempt(
        side="buy", decision_date="2006-01-31", attempt_date="2006-02-01",
        trading_days=sessions, open_price=10.0, locked_limit="up",
    )
    sell = evaluate_execution_attempt(
        side="sell", decision_date="2006-01-31", attempt_date="2006-02-01",
        trading_days=sessions, open_price=10.0, locked_limit="up",
    )
    assert buy.status == "deferred"
    assert sell.status == "executable"


def test_blocked_attempt_at_sample_end_is_cancelled():
    sessions = pd.DatetimeIndex(["2006-01-31", "2006-02-01"])
    result = evaluate_execution_attempt(
        side="sell", decision_date="2006-01-31", attempt_date="2006-02-01",
        trading_days=sessions, open_price=None, suspended=True,
    )
    assert result.status == "cancelled"
    assert result.deferred_to is None
    assert result.reason == "suspended"


def test_blocked_attempt_is_cancelled_when_next_session_exceeds_signal_expiry():
    sessions = pd.DatetimeIndex(["2006-01-31", "2006-02-01", "2006-02-02"])
    result = evaluate_execution_attempt(
        side="buy", decision_date="2006-01-31", attempt_date="2006-02-01",
        trading_days=sessions, open_price=None, valid_until="2006-02-01",
    )
    assert result.status == "cancelled"
    assert result.reason == "superseded_signal"
