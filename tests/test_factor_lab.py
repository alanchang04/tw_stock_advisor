"""
因子研究框架測試（research/factor_lab.py，SPEC_QUANT_UPGRADE.md P1）。
用小規模合成資料驗證 IC/十分位數/事件研究的計算邏輯本身是對的——這個工具是用來
下「這個因子有沒有用」的結論的，工具本身算錯比沒有工具更危險，所以要先證明對。
"""
import os
import sys
import datetime as _dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

from research.factor_lab import (
    forward_returns, compute_ic, ic_summary, decile_returns, decile_spread,
    decile_monotonic, event_car, factor_report,
)


def _dates(n):
    return [_dt.date(2024, 1, 1) + _dt.timedelta(days=i) for i in range(n)]


# ── forward_returns ─────────────────────────────────────────────
def test_forward_returns_basic():
    idx = _dates(4)
    closes = pd.DataFrame({"A": [100.0, 110.0, 121.0, 133.1]}, index=idx)
    fwd = forward_returns(closes, 1)
    assert fwd["A"].iloc[0] == pytest.approx(0.10)
    assert fwd["A"].iloc[1] == pytest.approx(0.10)
    assert pd.isna(fwd["A"].iloc[-1])   # 最後一天沒有未來資料


# ── compute_ic ───────────────────────────────────────────────────
def test_ic_perfect_positive_correlation():
    idx = _dates(3)
    # 每天因子排名完全跟未來報酬排名一致
    factor = pd.DataFrame({"A": [1, 2, 3], "B": [2, 4, 6], "C": [3, 6, 9]}, index=idx)
    fwd = pd.DataFrame({"A": [0.01, 0.02, 0.03], "B": [0.02, 0.04, 0.06],
                        "C": [0.03, 0.06, 0.09]}, index=idx)
    ic = compute_ic(factor, fwd)
    assert (ic.dropna() > 0.99).all()

def test_ic_perfect_negative_correlation():
    idx = _dates(3)
    factor = pd.DataFrame({"A": [1, 2, 3], "B": [2, 4, 6], "C": [3, 6, 9]}, index=idx)
    fwd = pd.DataFrame({"A": [0.09, 0.06, 0.03], "B": [0.09, 0.06, 0.03],
                        "C": [0.09, 0.06, 0.03]}, index=idx)
    ic = compute_ic(factor, fwd)
    assert (ic.dropna() < -0.99).all()

def test_ic_no_common_columns_returns_empty_or_nan():
    idx = _dates(2)
    factor = pd.DataFrame({"A": [1, 2]}, index=idx)
    fwd = pd.DataFrame({"B": [0.1, 0.2]}, index=idx)
    ic = compute_ic(factor, fwd)
    assert ic.isna().all() or ic.empty


# ── ic_summary ───────────────────────────────────────────────────
def test_ic_summary_basic_stats():
    ic = pd.Series([0.1] * 40)   # 穩定正向、無波動
    s = ic_summary(ic, min_days=30)
    assert s["n_days"] == 40
    assert s["ic_mean"] == pytest.approx(0.1)
    assert s["hit_rate"] == 1.0

def test_ic_summary_insufficient_days_returns_none_note():
    ic = pd.Series([0.1] * 5)
    s = ic_summary(ic, min_days=30)
    assert s["ic_mean"] is None
    assert "note" in s

def test_ic_summary_drops_nan_before_counting():
    ic = pd.Series([0.1] * 35 + [np.nan] * 100)
    s = ic_summary(ic, min_days=30)
    assert s["n_days"] == 35


# ── decile_returns / decile_spread / decile_monotonic ────────────
def test_decile_returns_monotonic_when_factor_predicts_return():
    idx = _dates(1)
    # 40檔股票，因子值1~40，未來報酬完全跟因子值同向（值越大報酬越高）
    stocks = [f"S{i}" for i in range(40)]
    factor = pd.DataFrame([list(range(40))], columns=stocks, index=idx)
    fwd = pd.DataFrame([[i * 0.001 for i in range(40)]], columns=stocks, index=idx)
    dec = decile_returns(factor, fwd, n_deciles=10, min_per_day=30)
    assert not dec.empty
    assert decile_monotonic(dec)
    spread = decile_spread(dec)
    assert spread > 0   # 最高分組報酬 > 最低分組

def test_decile_returns_skips_days_with_too_few_stocks():
    idx = _dates(1)
    factor = pd.DataFrame([[1, 2, 3]], columns=["A", "B", "C"], index=idx)
    fwd = pd.DataFrame([[0.01, 0.02, 0.03]], columns=["A", "B", "C"], index=idx)
    dec = decile_returns(factor, fwd, n_deciles=10, min_per_day=30)   # 只有3檔，門檻30
    assert dec.empty

def test_decile_spread_none_when_only_one_decile():
    dec = pd.DataFrame({"mean_ret": [0.05]}, index=[0])
    assert decile_spread(dec) is None

def test_decile_spread_empty_df_returns_none():
    assert decile_spread(pd.DataFrame(columns=["mean_ret", "n_obs"])) is None


# ── event_car ────────────────────────────────────────────────────
def test_event_car_computes_cumulative_excess_return():
    idx = _dates(10)
    # 股票A在day0觸發事件，之後5天每天超額報酬固定+1%
    closes = pd.DataFrame({"A": [100 * (1.01 ** i) for i in range(10)]}, index=idx)
    bench_ret = pd.Series(0.0, index=idx)   # 大盤完全平盤，超額報酬=自身報酬
    event_mask = pd.DataFrame({"A": [True] + [False] * 9}, index=idx)
    car = event_car(closes, event_mask, bench_ret, horizons=[1, 5])
    assert car.loc[1, "n_events"] == 1
    assert car.loc[1, "mean_car"] == pytest.approx(0.01, rel=0.01)
    # CAR是事件研究慣例的「逐日異常報酬直接加總」（不是複利），5天每天+1%加總=+5%，
    # 不是(1.01^5-1)那種複利算法——這是刻意選擇，多筆事件平均時加總比複利統計上更乾淨。
    assert car.loc[5, "mean_car"] == pytest.approx(0.05, rel=0.01)

def test_event_car_no_events_returns_empty():
    idx = _dates(5)
    closes = pd.DataFrame({"A": [100.0] * 5}, index=idx)
    bench_ret = pd.Series(0.0, index=idx)
    event_mask = pd.DataFrame({"A": [False] * 5}, index=idx)
    car = event_car(closes, event_mask, bench_ret)
    assert car.empty

def test_event_car_drops_events_too_close_to_data_end():
    idx = _dates(3)
    closes = pd.DataFrame({"A": [100.0, 101.0, 102.0]}, index=idx)
    bench_ret = pd.Series(0.0, index=idx)
    event_mask = pd.DataFrame({"A": [False, False, True]}, index=idx)   # 最後一天才觸發
    car = event_car(closes, event_mask, bench_ret, horizons=[5])
    assert car.loc[5, "n_events"] == 0   # 資料不夠長，這個事件被跳過不硬湊


# ── factor_report（整合，確認多時間窗都跑得動不報錯）────────────
def test_factor_report_runs_multiple_horizons_without_error():
    idx = _dates(30)
    stocks = [f"S{i}" for i in range(20)]
    rng = np.random.default_rng(42)
    factor = pd.DataFrame(rng.normal(size=(30, 20)), columns=stocks, index=idx)
    closes = pd.DataFrame(100 * np.cumprod(1 + rng.normal(0, 0.01, size=(30, 20)), axis=0),
                          columns=stocks, index=idx)
    report = factor_report("test_factor", factor, closes, horizons=[1, 5])
    assert report["factor"] == "test_factor"
    assert set(report["horizons"].keys()) == {1, 5}
    for h in (1, 5):
        assert "ic" in report["horizons"][h]
