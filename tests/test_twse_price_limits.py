"""TWSE 漲跌停判定的單元測試（合成 fixtures，不讀 snapshot、不計算報酬）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.twse_price_limits import (
    limit_prices,
    locked_limit_frame,
    reference_price,
    tick_size,
)


def test_tick_size_follows_official_bands():
    prices = np.array([9.99, 10.0, 49.95, 50.0, 99.9, 100.0, 499.5, 500.0, 999.0, 1000.0])
    expected = np.array([0.01, 0.05, 0.05, 0.1, 0.1, 0.5, 0.5, 1.0, 1.0, 5.0])
    assert np.allclose(tick_size(prices), expected)


def test_reference_price_recovers_the_official_denominator():
    # 收盤 10.70、漲幅 7% → 參考價必須回到 10.00
    assert reference_price(np.array([10.70]), np.array([7.0]))[0] == pytest.approx(10.0)


def test_limit_price_rounds_down_to_a_legal_tick():
    # 參考價 10.05：10.05*1.07=10.7535，檔位 0.05 → 漲停 10.75（+6.965%），不是 10.7535
    upper, _ = limit_prices(np.array([10.05]))
    assert upper[0] == pytest.approx(10.75)


def test_band_crossing_pushes_the_limit_well_below_seven_percent():
    """這正是 change_pct 門檻法失效的原因，必須被鎖住。"""
    upper, _ = limit_prices(np.array([9.99]))
    assert upper[0] == pytest.approx(10.65)
    assert (upper[0] / 9.99 - 1) * 100 == pytest.approx(6.607, abs=0.01)


def test_limit_down_rounds_up_to_a_legal_tick():
    _, lower = limit_prices(np.array([10.05]))
    assert lower[0] == pytest.approx(9.35)      # 10.05*0.93=9.3465 → 檔位 0.01 → 9.35


def frame(values, columns=("1101",), index=None):
    index = index if index is not None else pd.DatetimeIndex(["2006-01-03"])
    return pd.DataFrame(values, index=index, columns=list(columns))


def build(open_, high, low, close, change_pct):
    return locked_limit_frame(
        open_=frame([[open_]]), high=frame([[high]]), low=frame([[low]]),
        close=frame([[close]]), change_pct=frame([[change_pct]]),
    ).iloc[0, 0]


def test_single_price_at_limit_up_is_locked():
    # 參考價 10.00 → 漲停 10.70；整日單一價
    assert build(10.70, 10.70, 10.70, 10.70, 7.0) == "up"


def test_single_price_at_limit_down_is_locked():
    assert build(9.30, 9.30, 9.30, 9.30, -7.0) == "down"


def test_limit_close_with_intraday_trading_below_is_not_locked():
    """曾在漲停價以外成交代表有對手盤，開盤市價單本來就會成交，不得阻擋。"""
    assert build(10.50, 10.70, 10.45, 10.70, 7.0) is None


def test_single_price_below_the_limit_is_not_locked():
    """整日單一價但不在漲停價（冷門股只成交一筆），不是鎖死。"""
    assert build(10.50, 10.50, 10.50, 10.50, 5.0) is None


def test_band_crossing_limit_is_still_detected():
    """參考價 9.99 的漲停是 10.65（+6.61%），門檻法會漏掉，這裡必須抓到。"""
    assert build(10.65, 10.65, 10.65, 10.65, 6.60660660660661) == "up"


def test_degenerate_penny_price_returns_none_instead_of_guessing():
    """0.28 元的股票 7% 不足一個檔位，漲跌停概念退化，不得硬套。"""
    assert build(0.07, 0.07, 0.07, 0.07, 0.0) is None


def test_missing_price_yields_none():
    assert build(np.nan, np.nan, np.nan, np.nan, np.nan) is None


def test_shape_mismatch_is_rejected():
    index = pd.DatetimeIndex(["2006-01-03"])
    with pytest.raises(ValueError, match="形狀"):
        locked_limit_frame(
            open_=frame([[10.0]], index=index),
            high=frame([[10.0]], index=index),
            low=frame([[10.0]], index=index),
            close=frame([[10.0]], index=index),
            change_pct=pd.DataFrame([[0.0, 0.0]], index=index, columns=["1101", "1102"]),
        )


def test_frame_keeps_index_and_columns():
    index = pd.DatetimeIndex(["2006-01-03", "2006-01-04"])
    cols = ["1101", "1102"]
    zeros = pd.DataFrame(10.0, index=index, columns=cols)
    result = locked_limit_frame(
        open_=zeros, high=zeros, low=zeros, close=zeros,
        change_pct=pd.DataFrame(0.0, index=index, columns=cols),
    )
    assert result.index.equals(index)
    assert list(result.columns) == cols
    assert result.isna().all().all() or (result == None).all().all()  # noqa: E711
