"""SPEC §4.4 統計檢定的測試。

重點是驗「已知答案的情境」——用建構出來的資料檢查函式會不會給出正確的判決，
特別是**該說不顯著的時候要說不顯著**（統計工具最危險的失效模式是過度樂觀）。
"""
import numpy as np
import pandas as pd
import pytest

from research.stats_tests import (bootstrap_mean_ci, deflated_sharpe,
                                  deflated_sharpe_sensitivity, excess_return_ttest,
                                  expected_max_sharpe, format_report, monte_carlo_mdd,
                                  run_all)


# ── bootstrap ────────────────────────────────────────────────────
def test_bootstrap_clearly_positive_series_is_significant():
    r = np.full(200, 0.05) + np.random.default_rng(0).normal(0, 0.001, 200)
    out = bootstrap_mean_ci(r)
    assert out["significant"] and out["ci_low"] > 0
    assert out["mean"] == pytest.approx(0.05, abs=0.005)


def test_bootstrap_noise_around_zero_is_not_significant():
    r = np.random.default_rng(1).normal(0, 0.1, 300)
    out = bootstrap_mean_ci(r)
    assert not out["significant"]
    assert out["ci_low"] < 0 < out["ci_high"]


def test_bootstrap_fat_tail_few_winners_widens_ci():
    """策略的真實形狀：大多小虧、少數暴賺。CI 應該寬到跨過 0。"""
    r = np.concatenate([np.full(96, -0.07), np.array([3.0, 2.0, 1.5, 1.0])])
    out = bootstrap_mean_ci(r)
    assert out["mean"] > 0                      # 平均是正的
    assert out["ci_low"] < 0                    # 但區間跨 0
    assert not out["significant"]               # → 不能宣稱顯著


def test_bootstrap_ci_is_ordered_and_contains_mean():
    out = bootstrap_mean_ci(np.random.default_rng(2).normal(0.02, 0.1, 200))
    assert out["ci_low"] < out["mean"] < out["ci_high"]


def test_bootstrap_handles_degenerate_input():
    assert bootstrap_mean_ci([]).get("n") == 0
    assert bootstrap_mean_ci([0.1])["n"] == 1
    assert bootstrap_mean_ci([0.1, np.nan, 0.2])["n"] == 2   # NaN 要被剔掉


def test_bootstrap_is_deterministic_with_seed():
    a = bootstrap_mean_ci([0.1, -0.05, 0.2, -0.03], seed=7)
    b = bootstrap_mean_ci([0.1, -0.05, 0.2, -0.03], seed=7)
    assert a == b


# ── 蒙地卡羅回撤 ──────────────────────────────────────────────────
def test_monte_carlo_mdd_tail_is_worse_than_median():
    r = np.random.default_rng(3).normal(0.01, 0.15, 200)
    out = monte_carlo_mdd(r, n_sims=500)
    assert out["mdd_p95"] <= out["mdd_median"]          # 回撤是負數，95分位更負
    assert out["mdd_worst"] <= out["mdd_p99"] <= out["mdd_p95"]


def test_monte_carlo_all_winners_has_no_drawdown():
    out = monte_carlo_mdd(np.full(50, 0.05), n_sims=100)
    assert out["mdd_median"] == pytest.approx(0.0, abs=1e-9)


def test_monte_carlo_position_weight_scales_drawdown():
    """部位越小，同一批交易造成的回撤越淺——這是檢查權重真的有生效。"""
    r = np.random.default_rng(4).normal(0.0, 0.2, 150)
    big = monte_carlo_mdd(r, n_sims=300, position_weight=0.5)
    small = monte_carlo_mdd(r, n_sims=300, position_weight=0.05)
    assert big["mdd_median"] < small["mdd_median"]


# ── t 檢定 ───────────────────────────────────────────────────────
def _nav(rets, start=100.0):
    idx = pd.date_range("2020-01-01", periods=len(rets) + 1)
    return dict(zip(idx.date, np.concatenate([[start], start * np.cumprod(1 + np.asarray(rets))])))


def test_ttest_detects_real_outperformance():
    rng = np.random.default_rng(5)
    bench = rng.normal(0.0003, 0.01, 800)
    strat = bench + 0.002                        # 每日穩定多賺 20bp
    out = excess_return_ttest(_nav(strat), _nav(bench))
    assert out["significant"] and out["t_stat"] > 2


def test_ttest_says_not_significant_when_only_noise():
    rng = np.random.default_rng(6)
    bench = rng.normal(0.0003, 0.012, 800)
    strat = bench + rng.normal(0, 0.012, 800)    # 純雜訊差異
    assert not excess_return_ttest(_nav(strat), _nav(bench))["significant"]


def test_ttest_handles_missing_benchmark():
    out = excess_return_ttest(_nav([0.01] * 10), None)
    assert not out["significant"] and np.isnan(out["t_stat"])


# ── deflated Sharpe ──────────────────────────────────────────────
def test_expected_max_sharpe_grows_with_trials():
    """試越多次，純運氣能刷出的最大 Sharpe 越高——這是 deflation 的核心直覺。"""
    assert expected_max_sharpe(5, 0.5) < expected_max_sharpe(60, 0.5) \
        < expected_max_sharpe(1000, 0.5)


def test_expected_max_sharpe_degenerate():
    assert expected_max_sharpe(1, 0.5) == 0.0
    assert expected_max_sharpe(50, 0.0) == 0.0


def test_deflated_sharpe_penalises_many_trials():
    """同一組報酬，宣稱只試 2 次 vs 試 200 次，後者的 DSR 必須更低。"""
    r = np.random.default_rng(7).normal(0.0008, 0.012, 2000)
    few = deflated_sharpe(r, n_trials=2)
    many = deflated_sharpe(r, n_trials=200)
    assert few["dsr"] > many["dsr"]
    assert few["sr_threshold"] < many["sr_threshold"]


def test_deflated_sharpe_rejects_pure_noise():
    r = np.random.default_rng(8).normal(0.0, 0.012, 1500)
    assert not deflated_sharpe(r, n_trials=58)["passes"]


def test_deflated_sharpe_degenerate_input():
    assert not deflated_sharpe([0.01, 0.01], n_trials=10)["passes"]
    assert not deflated_sharpe(np.zeros(100), n_trials=10)["passes"]


def test_deflated_sharpe_threshold_is_in_plausible_annual_range():
    """單位 bug 回歸測試（2026-07-24）：第一版把年化 σ 直接餵進每期公式，
    算出「純運氣門檻 年化 Sharpe 18.5」這種不可能的數字，DSR 恆為 0。
    58 次試驗、σ=0.5 的年化門檻應該落在 1 上下，絕不可能是兩位數。"""
    r = np.random.default_rng(11).normal(0.0005, 0.012, 2500)
    out = deflated_sharpe(r, n_trials=58, sharpe_std_ann=0.5)
    assert 0.5 < out["sr_threshold_ann"] < 3.0, \
        f"門檻 {out['sr_threshold_ann']:.2f} 不在合理年化 Sharpe 範圍——單位可能又錯了"


def test_deflated_sharpe_threshold_scales_with_assumed_std():
    r = np.random.default_rng(12).normal(0.0005, 0.012, 2000)
    lo = deflated_sharpe(r, 58, sharpe_std_ann=0.1)["sr_threshold_ann"]
    hi = deflated_sharpe(r, 58, sharpe_std_ann=0.5)["sr_threshold_ann"]
    assert hi > lo > 0
    assert hi / lo == pytest.approx(5.0, rel=1e-6)   # 門檻與 σ 成正比


def test_sensitivity_returns_one_row_per_std():
    r = np.random.default_rng(13).normal(0.0005, 0.012, 1500)
    rows = deflated_sharpe_sensitivity(r, 58, stds=(0.1, 0.3, 0.5))
    assert [x["sharpe_std_ann"] for x in rows] == [0.1, 0.3, 0.5]
    # σ 越大門檻越嚴 → DSR 單調遞減
    assert rows[0]["dsr"] >= rows[1]["dsr"] >= rows[2]["dsr"]


def test_report_flags_when_conclusion_flips_with_assumption():
    """結論隨假設翻轉時必須明講——這正是 §4.3「高原不是尖峰」的同一種紀律。"""
    res = {"bootstrap": bootstrap_mean_ci([0.05] * 50),
           "monte_carlo": monte_carlo_mdd([0.05, -0.03] * 25, n_sims=50),
           "ttest": excess_return_ttest(None, None),
           "deflated_sharpe": {"n_trials": 58, "sharpe_ann": 1.0, "dsr": 0.5,
                               "passes": False, "sr_threshold_ann": 1.0},
           "dsr_sensitivity": [
               {"sharpe_std_ann": 0.1, "sr_threshold_ann": 0.2, "dsr": 0.99, "passes": True},
               {"sharpe_std_ann": 0.5, "sr_threshold_ann": 1.2, "dsr": 0.10, "passes": False},
           ]}
    assert "結論隨假設翻轉" in format_report(res)


# ── 彙總與報告 ────────────────────────────────────────────────────
def test_run_all_and_format_report_do_not_crash():
    rng = np.random.default_rng(9)
    bench = rng.normal(0.0004, 0.011, 600)
    strat = bench + rng.normal(0.0001, 0.011, 600)
    res = run_all(trade_returns=list(rng.normal(0.05, 0.3, 120)),
                  nav=_nav(strat), nav_bench=_nav(bench), n_trials=58)
    txt = format_report(res, mdd_actual=-0.274)
    for key in ("bootstrap", "monte_carlo", "ttest", "deflated_sharpe"):
        assert key in res
    for word in ("信賴區間", "蒙地卡羅", "t 檢定", "Deflated Sharpe"):
        assert word in txt
