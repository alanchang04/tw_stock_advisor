"""MOM-1 named-release adapter unit tests (synthetic, no holdout metrics)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.momentum import compute_mom_6_1
from research.momentum_release import build_backward_adjusted_close


def test_backward_adjustment_applies_only_before_event_date():
    dates = pd.bdate_range("2006-01-02", periods=130)
    raw = pd.DataFrame({"1101": np.arange(10.0, 140.0)}, index=dates)
    event_date = dates[100]
    actions = pd.DataFrame({
        "stock_id": ["1101"],
        "event_date": [event_date],
        "adjustment_factor": [0.5],
    })
    adjusted = build_backward_adjusted_close(raw, actions)
    assert adjusted.loc[dates[99], "1101"] == pytest.approx(raw.loc[dates[99], "1101"] * 0.5)
    assert adjusted.loc[event_date, "1101"] == raw.loc[event_date, "1101"]
    assert adjusted.loc[dates[101], "1101"] == raw.loc[dates[101], "1101"]


def test_future_action_rescales_both_formation_endpoints_and_cancels():
    dates = pd.bdate_range("2006-01-02", periods=180)
    raw = pd.DataFrame({"1101": np.linspace(20.0, 40.0, len(dates))}, index=dates)
    decision = dates[140]
    future_event = pd.DataFrame({
        "stock_id": ["1101"],
        "event_date": [dates[160]],
        "adjustment_factor": [0.25],
    })
    baseline = compute_mom_6_1(raw).loc[decision, "1101"]
    adjusted = build_backward_adjusted_close(raw, future_event)
    assert compute_mom_6_1(adjusted).loc[decision, "1101"] == pytest.approx(baseline)


def test_backward_adjustment_rejects_duplicate_stock_event_date():
    dates = pd.bdate_range("2006-01-02", periods=2)
    raw = pd.DataFrame({"1101": [10.0, 11.0]}, index=dates)
    actions = pd.DataFrame({
        "stock_id": ["1101", "1101"],
        "event_date": [dates[1], dates[1]],
        "adjustment_factor": [0.5, 0.5],
    })
    with pytest.raises(ValueError, match="多個調整因子"):
        build_backward_adjusted_close(raw, actions)


def test_backward_adjustment_allows_different_stocks_on_same_event_date():
    dates = pd.bdate_range("2006-01-02", periods=2)
    raw = pd.DataFrame({"1101": [10.0, 11.0], "1102": [20.0, 21.0]}, index=dates)
    actions = pd.DataFrame({
        "stock_id": ["1101", "1102"],
        "event_date": [dates[1], dates[1]],
        "adjustment_factor": [0.5, 0.25],
    })
    adjusted = build_backward_adjusted_close(raw, actions)
    assert adjusted.loc[dates[0], "1101"] == pytest.approx(5.0)
    assert adjusted.loc[dates[0], "1102"] == pytest.approx(5.0)
