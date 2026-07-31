import pandas as pd
import pytest

from agent.performance import buy_and_hold_nav, compound_trade_returns, metric_table
from agent.backtest import perf_metrics


def test_buy_hold_uses_integer_shares_and_liquidates_with_costs():
    px = pd.Series([100.0, 110.0], index=pd.date_range("2024-01-01", periods=2))
    nav = buy_and_hold_nav(px, 1_000.0, fee_rate=0.01, tax_rate=0.02, slippage=0.0)
    # 9 shares, 91 cash; final liquidation is 9*110*(1-.01-.02)+91
    assert nav.iloc[-1] == pytest.approx(1051.3)


def test_compound_report_does_not_sum_returns():
    nav = compound_trade_returns([0.10, -0.10])
    assert nav.iloc[-1] == pytest.approx(0.99)


def test_metric_table_has_same_columns_for_strategy_and_benchmark():
    table = metric_table({"total": .2, "mdd": -.1}, {"total": .3, "mdd": -.2})
    assert list(table.index) == ["strategy", "0050"]
    assert table.loc["0050", "total"] == .3


def test_metrics_annualize_by_calendar_span_not_row_count():
    nav = pd.Series([100.0, 105.0, 110.0],
                    index=pd.to_datetime(["2020-01-02", "2022-01-03", "2024-01-02"]))
    metrics = perf_metrics(nav)
    assert metrics["ann_ret"] == pytest.approx(1.1 ** (1/4) - 1, rel=.01)
    assert metrics["coverage"] < .01
