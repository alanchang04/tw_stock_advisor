import numpy as np
import pandas as pd

from margin_reversal.signals import build_feature_panel, select_signals
from margin_reversal.backtest import run_margin_reversal_backtest
from margin_reversal.event_study import add_forward_returns, matched_margin_wash_study
from margin_reversal.report import format_event_study


def _panel_input():
    dates = pd.date_range("2024-01-01", periods=30, freq="B")
    rows = []
    for sid, wash in [("1111", True), ("2222", False), ("0050", True)]:
        for i, d in enumerate(dates):
            close = 100-i*1.1 if i < 24 else 76+(i-24)*3
            rows.append({"stock_id": sid, "trade_date": d,
                         "close": close, "open": close, "high": close, "low": close,
                         "margin_balance": (3000-i*100 if wash and i >= 20 else 3000-i),
                         "inst_net": 100 if i >= 20 else -1,
                         "revenue_yoy": 10.0, "rsi14": 30.0,
                         "turnover": 50_000_000,
                         "asset_type": "etf" if sid == "0050" else "common_stock"})
    market = pd.Series(np.r_[np.repeat(100., 20), np.linspace(97, 85, 10)], index=dates)
    return pd.DataFrame(rows), market


def test_signal_is_point_in_time_and_excludes_etf():
    raw, market = _panel_input()
    panel = build_feature_panel(raw, market,
                                {"margin_drop_5d": -0.10, "price_drawdown_20d": -0.10})
    selected = select_signals(panel, top_n=10)
    assert "1111" in set(selected["stock_id"])
    assert "0050" not in set(selected["stock_id"])
    assert not any(c.startswith("fwd_") for c in panel.columns)


def test_backtest_fills_next_open_and_never_overspends():
    dates = pd.date_range("2024-01-01", periods=5, freq="B")
    prices = pd.DataFrame({"stock_id": ["1111"]*5, "trade_date": dates,
                           "open": [100, 101, 103, 105, 106],
                           "close": [100, 102, 104, 106, 107]})
    signals = pd.DataFrame({"stock_id": ["1111"], "trade_date": [dates[0]], "score": [10]})
    res = run_margin_reversal_backtest(prices, signals,
                                       {"capital": 10_000, "max_hold_days": 2,
                                        "position_fraction": 1.0})
    trade = res["trades"].iloc[0]
    assert trade["entry_date"] == dates[1]
    assert trade["entry_price"] > 101
    assert trade["exit_date"] == dates[4]  # day-3 close signal, next-open exit
    assert res["nav"].min() >= 0


def test_matched_event_study_measures_margin_wash_lift():
    dates = pd.date_range("2024-01-01", periods=8, freq="B")
    rows = []
    for sid, wash, returns in [("1111", True, [100,100,100,100,100,110,111,112]),
                               ("2222", False,[100,100,100,100,100,101,101,101])]:
        for d, close in zip(dates, returns):
            rows.append({"stock_id": sid, "trade_date": d, "close": close,
                         "market_stress": True, "price_oversold": True,
                         "fundamental_ok": True, "institutional_ok": True,
                         "margin_wash": wash, "price_drawdown_20d": -.2,
                         "revenue_yoy": 10.0})
    panel = add_forward_returns(pd.DataFrame(rows), (1,))
    result = matched_margin_wash_study(panel[panel["trade_date"].eq(dates[4])], horizon=1)
    assert result["treated_n"] == 1
    assert result["lift"] > 0
    assert "配對 1 組" in format_event_study(result)


def test_report_calls_significantly_negative_lift_rejected():
    result = {"horizon": 5, "treated_n": 20, "treated_mean": -.02,
              "control_mean": .01, "lift": -.03, "ci_low": -.05,
              "ci_high": -.01, "direction": "worse"}
    assert "顯著劣於對照，否決" in format_event_study(result)
