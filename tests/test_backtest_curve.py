"""回測曲線衍生序列的單元測試（合成 fixtures）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from agent.backtest_curve import (
    concentration_summary,
    cumulative_pnl_excluding_top,
    drawdown,
    monthly_returns,
    rolling_return,
)


def test_drawdown_is_zero_at_new_highs_and_negative_after():
    nav = pd.Series([100.0, 110.0, 99.0, 121.0])
    dd = drawdown(nav)
    assert dd.iloc[0] == 0.0
    assert dd.iloc[1] == 0.0                       # 新高
    assert dd.iloc[2] == pytest.approx(99 / 110 - 1)
    assert dd.iloc[3] == 0.0                       # 再創新高後歸零


def test_drawdown_uses_running_peak_not_final_peak():
    nav = pd.Series([100.0, 50.0, 200.0])
    assert drawdown(nav).iloc[1] == pytest.approx(-0.5)


def nav_frame(values, start="2015-01-05"):
    idx = pd.bdate_range(start, periods=len(values))
    return pd.DataFrame({"trade_date": idx, "strategy_nav": list(map(float, values))})


def test_monthly_returns_keeps_the_first_month():
    """以月末對月末計算會平白丟掉第一個月，這裡必須保留。"""
    frame = nav_frame(np.linspace(100, 200, 70))
    table = monthly_returns(frame)
    assert not table.isna().all().all()
    first_year = table.index.min()
    assert table.loc[first_year].notna().any()


def test_monthly_returns_shape_is_year_by_month():
    frame = nav_frame(np.linspace(100, 300, 400))
    table = monthly_returns(frame)
    assert table.index.name == "year"
    assert set(table.columns).issubset(set(range(1, 13)))


def test_rolling_return_is_nan_before_the_window_is_full():
    frame = nav_frame(np.linspace(100, 200, 300))
    roll = rolling_return(frame, "strategy_nav", window_sessions=252)
    assert roll.iloc[:252].isna().all()
    assert roll.iloc[252:].notna().all()


def trades_frame(pnls, dates=None):
    dates = dates or pd.bdate_range("2015-01-05", periods=len(pnls))
    return pd.DataFrame({
        "exit_date": pd.to_datetime(dates),
        "net_pnl": list(map(float, pnls)),
    })


def test_excluding_top_trades_lowers_the_final_cumulative_pnl():
    trades = trades_frame([100, 50, 1000, -30, 20])
    result = cumulative_pnl_excluding_top(trades, exclude_counts=(0, 1))
    assert result["完整"].iloc[-1] == pytest.approx(1140.0)
    assert result["移除最佳 1 筆"].iloc[-1] == pytest.approx(140.0)


def test_exclusion_removes_exactly_the_largest_gains():
    trades = trades_frame([10, 900, 800, 5])
    result = cumulative_pnl_excluding_top(trades, exclude_counts=(0, 2))
    assert result["完整"].iloc[-1] == pytest.approx(1715.0)
    assert result["移除最佳 2 筆"].iloc[-1] == pytest.approx(15.0)


def test_pnl_curve_is_additive_not_compounded():
    """刻意使用可加的已實現損益；若改用 NAV 就只能近似。"""
    trades = trades_frame([100, 100, 100])
    result = cumulative_pnl_excluding_top(trades, exclude_counts=(0,))
    assert list(result["完整"]) == [100.0, 200.0, 300.0]


def test_same_day_exits_are_aggregated():
    day = pd.Timestamp("2015-03-05")
    trades = pd.DataFrame({"exit_date": [day, day], "net_pnl": [10.0, 20.0]})
    result = cumulative_pnl_excluding_top(trades, exclude_counts=(0,))
    assert len(result) == 1
    assert result["完整"].iloc[0] == pytest.approx(30.0)


def test_missing_columns_are_rejected():
    with pytest.raises(ValueError, match="net_pnl"):
        cumulative_pnl_excluding_top(pd.DataFrame({"exit_date": []}))


def test_concentration_summary_reports_top_share():
    trades = trades_frame([1000, 10, 10, -20])
    summary = concentration_summary(trades, top_n=1)
    assert summary["trades"] == 4
    assert summary["total_net_pnl"] == pytest.approx(1000.0)
    assert summary["top_n_share_of_net"] == pytest.approx(1.0)
    assert summary["win_rate"] == pytest.approx(0.75)
