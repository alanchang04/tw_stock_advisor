"""H01 布林二次突破事件偵測的單元測試（合成 fixtures）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.bollinger import (
    MAX_PERIODS_BETWEEN_BREAKS,
    bollinger_bands,
    detect_all,
    detect_rebreakouts,
    weekly_regime,
)

WARMUP = 90


def series_from(tail_values, base=100.0, warmup=WARMUP):
    """前段為固定價（讓 BB 收斂），後段接上指定走勢。"""
    idx = pd.bdate_range("2015-01-05", periods=warmup + len(tail_values))
    values = [base] * warmup + list(tail_values)
    return pd.Series(values, index=idx, dtype=float)


def volume_for(close, level=1e6):
    return pd.Series(level, index=close.index)


def test_bands_are_symmetric_around_the_middle():
    close = series_from([100.0] * 10)
    middle, upper, lower = bollinger_bands(close)
    assert (upper - middle).iloc[-1] == pytest.approx((middle - lower).iloc[-1])


def test_single_breakout_without_retreat_is_not_an_event():
    """只有第一次突破、沒有回縮再突破 → 不是本假說的事件。"""
    close = series_from([120.0] * 5)
    events = detect_rebreakouts(close, volume_for(close), side="upper")
    assert events.empty


def test_break_retreat_rebreak_produces_exactly_one_event():
    # 突破 → 回到帶內 → 再突破
    close = series_from([130.0, 100.0, 100.0, 140.0])
    events = detect_rebreakouts(close, volume_for(close), side="upper")
    assert len(events) == 1
    assert events["event_date"].iloc[0] == close.index[-1]
    assert events["first_break_date"].iloc[0] == close.index[WARMUP]


def test_event_is_the_second_break_not_the_first():
    close = series_from([130.0, 100.0, 140.0])
    events = detect_rebreakouts(close, volume_for(close), side="upper")
    assert events["event_date"].iloc[0] != events["first_break_date"].iloc[0]
    assert events["event_date"].iloc[0] > events["first_break_date"].iloc[0]


def test_low_volume_first_break_does_not_arm_the_state_machine():
    close = series_from([130.0, 100.0, 140.0])
    volume = volume_for(close)
    volume.iloc[WARMUP] = 1.0          # 第一次突破當日量能不足
    events = detect_rebreakouts(close, volume, side="upper")
    assert events.empty


def test_second_break_beyond_the_limit_is_voided():
    """§3.1：超過 60 期未再突破即作廢，不算同一個 setup。"""
    gap = MAX_PERIODS_BETWEEN_BREAKS + 5
    close = series_from([130.0] + [100.0] * gap + [140.0])
    events = detect_rebreakouts(close, volume_for(close), side="upper")
    assert events.empty


def test_second_break_within_the_limit_is_kept():
    gap = MAX_PERIODS_BETWEEN_BREAKS - 5
    close = series_from([130.0] + [100.0] * gap + [140.0])
    events = detect_rebreakouts(close, volume_for(close), side="upper")
    assert len(events) == 1
    assert events["periods_between"].iloc[0] <= MAX_PERIODS_BETWEEN_BREAKS


def test_lower_side_is_symmetric():
    close = series_from([70.0, 100.0, 60.0])
    events = detect_rebreakouts(close, volume_for(close), side="lower")
    assert len(events) == 1


def test_invalid_side_is_rejected():
    close = series_from([100.0])
    with pytest.raises(ValueError, match="side"):
        detect_rebreakouts(close, volume_for(close), side="sideways")


def test_short_series_returns_no_events_instead_of_raising():
    idx = pd.bdate_range("2015-01-05", periods=10)
    close = pd.Series(100.0, index=idx)
    events = detect_rebreakouts(close, pd.Series(1e6, index=idx), side="upper")
    assert events.empty


def test_detect_all_tags_each_stock():
    close = pd.DataFrame({
        "1101": series_from([130.0, 100.0, 140.0]),
        "1102": series_from([100.0, 100.0, 100.0]),
    })
    volume = pd.DataFrame(1e6, index=close.index, columns=close.columns)
    events = detect_all(close, volume, side="upper")
    assert set(events["stock_id"]) == {"1101"}


def test_weekly_regime_never_uses_the_current_week():
    """用上一週狀態，避免把當週尚未結束的資訊帶進來。"""
    idx = pd.bdate_range("2015-01-05", periods=200)
    close = pd.DataFrame({"1101": np.linspace(100, 200, 200)}, index=idx)
    regime = weekly_regime(close)
    assert regime.index.equals(idx)
    assert regime["1101"].iloc[0] == False        # noqa: E712 — 暖身期不得為 True
