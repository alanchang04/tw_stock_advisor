"""§8.2／§8.3／§9.2 指標與 F1 判準的單元測試（合成 fixtures）。

F1 是一次性的，判準算錯就等於用錯的門檻做了不可逆的判決。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.performance import (
    MONTHS_PER_BLOCK,
    block_bootstrap_monthly,
    core_metrics,
    f1_criteria,
    max_drawdown,
    monthly_profile,
    monthly_returns,
    relative_metrics,
    trade_concentration,
)

SESSIONS = pd.bdate_range("2008-01-01", periods=252 * 7)


def nav_from_daily(rate: float) -> pd.Series:
    return pd.Series(100_000.0 * (1 + rate) ** np.arange(len(SESSIONS)), index=SESSIONS)


class TestCoreMetrics:
    def test_a_constant_growth_series_has_matching_total_and_cagr(self):
        nav = nav_from_daily(0.0002)
        metrics = core_metrics(nav)
        implied = (1 + metrics["cagr"]) ** (len(SESSIONS) / 252) - 1
        assert implied == pytest.approx(metrics["total_return"], rel=1e-6)

    def test_a_monotonic_series_has_no_drawdown(self):
        assert core_metrics(nav_from_daily(0.0002))["max_drawdown"] == pytest.approx(0.0)

    def test_drawdown_is_measured_from_the_running_peak(self):
        nav = pd.Series([100.0, 120.0, 60.0, 90.0], index=SESSIONS[:4])
        assert max_drawdown(nav) == pytest.approx(-0.5)

    def test_the_longest_underwater_stretch_is_reported(self):
        nav = pd.Series([100.0, 90.0, 95.0, 99.0, 101.0, 100.0], index=SESSIONS[:6])
        assert core_metrics(nav)["longest_underwater_sessions"] == 3


class TestRelativeMetrics:
    def test_identical_series_have_zero_excess_and_unit_beta(self):
        nav = nav_from_daily(0.0003)
        relative = relative_metrics(nav, nav.copy())
        assert relative["annualised_excess"] == pytest.approx(0.0, abs=1e-9)
        assert relative["beta"] == pytest.approx(1.0)

    def test_outperformance_produces_positive_excess(self):
        relative = relative_metrics(nav_from_daily(0.0004), nav_from_daily(0.0002))
        assert relative["annualised_excess"] > 0
        assert relative["information_ratio"] > 0

    def test_beta_of_a_doubled_benchmark_is_two(self):
        benchmark = pd.Series(
            100.0 * np.cumprod(1 + np.random.default_rng(1).normal(0, 0.01, 600)),
            index=SESSIONS[:600])
        benchmark_returns = benchmark.pct_change().fillna(0.0)
        strategy = 100.0 * np.cumprod(1 + 2 * benchmark_returns)
        assert relative_metrics(strategy, benchmark)["beta"] == pytest.approx(2.0, rel=0.05)


class TestMonthlyProfile:
    def test_monthly_returns_use_month_end_nav(self):
        nav = nav_from_daily(0.0002)
        monthly = monthly_returns(nav)
        assert len(monthly) > 60
        assert (monthly > 0).all()

    def test_win_rate_and_annual_returns_are_reported(self):
        profile = monthly_profile(nav_from_daily(0.0002))
        assert profile["monthly_win_rate"] == pytest.approx(1.0)
        assert len(profile["annual_returns"]) >= 6


class TestTradeConcentration:
    @staticmethod
    def trades(values, years=None):
        years = years or [2010] * len(values)
        return pd.DataFrame({
            "net_pnl": values,
            "exit_date": [pd.Timestamp(f"{y}-06-30") for y in years],
        })

    def test_removing_the_best_five_can_flip_the_total_negative(self):
        """舊策略正是這個形狀：475 筆裡 10 筆決定 96% 的結果。"""
        result = trade_concentration(self.trades([100.0] * 5 + [-5.0] * 20))
        assert result["net_pnl"] == pytest.approx(400.0)
        assert result["net_pnl_excluding_best_5"] == pytest.approx(-100.0)

    def test_a_broad_book_survives_removing_the_best_five(self):
        result = trade_concentration(self.trades([10.0] * 40))
        assert result["net_pnl_excluding_best_5"] > 0

    def test_single_year_share_of_gross_profit_is_computed(self):
        result = trade_concentration(
            self.trades([90.0, 5.0, 5.0], years=[2011, 2012, 2013]))
        assert result["largest_year_share_of_gross_profit"] == pytest.approx(0.9)

    def test_an_empty_book_reports_zero_rather_than_raising(self):
        assert trade_concentration(pd.DataFrame())["trades"] == 0


class TestBlockBootstrap:
    def test_the_block_length_is_the_frozen_six_months(self):
        result = block_bootstrap_monthly(nav_from_daily(0.0003), draws=200)
        assert result["block_months"] == MONTHS_PER_BLOCK

    def test_a_confidence_interval_is_reported_not_just_a_point_estimate(self):
        result = block_bootstrap_monthly(nav_from_daily(0.0003), draws=200)
        low, high = result["annualised_return_ci95"]
        assert low < high

    def test_a_short_series_is_refused_rather_than_bootstrapped(self):
        short = pd.Series(100.0 * np.arange(1, 40), index=SESSIONS[:39])
        assert block_bootstrap_monthly(short)["draws"] == 0


class TestF1Criteria:
    @staticmethod
    def book(values, years):
        return pd.DataFrame({
            "net_pnl": values,
            "exit_date": [pd.Timestamp(f"{y}-06-30") for y in years],
        })

    def test_a_clearly_better_strategy_passes_every_check(self):
        nav = nav_from_daily(0.0004)
        benchmark = nav_from_daily(0.0001)
        trades = self.book([10.0] * 30, [2009 + i % 6 for i in range(30)])
        result = f1_criteria(nav=nav, benchmark=benchmark, trades=trades)
        assert result["passed"]
        assert all(result["checks"].values())

    def test_losing_to_the_benchmark_fails_the_excess_check(self):
        result = f1_criteria(nav=nav_from_daily(0.0001),
                             benchmark=nav_from_daily(0.0004),
                             trades=self.book([10.0] * 30,
                                              [2009 + i % 6 for i in range(30)]))
        assert not result["checks"]["annualised_excess_positive"]
        assert not result["passed"]

    def test_concentration_in_one_year_fails_even_when_returns_are_good(self):
        """單一年份佔毛利 > 40% 直接不通過，與報酬多好無關。"""
        nav = nav_from_daily(0.0004)
        trades = self.book([100.0] * 5 + [1.0] * 5, [2009] * 5 + [2010] * 5)
        result = f1_criteria(nav=nav, benchmark=nav_from_daily(0.0001), trades=trades)
        assert not result["checks"]["no_year_over_40pct_of_gross_profit"]
        assert not result["passed"]

    def test_a_book_carried_by_five_trades_fails(self):
        nav = nav_from_daily(0.0004)
        trades = self.book([100.0] * 5 + [-5.0] * 20,
                           [2009 + i % 6 for i in range(25)])
        result = f1_criteria(nav=nav, benchmark=nav_from_daily(0.0001), trades=trades)
        assert not result["checks"]["positive_excluding_best_5_trades"]

    def test_a_deeper_drawdown_than_the_benchmark_by_over_5pp_fails(self):
        base = np.r_[np.linspace(100.0, 200.0, 300), np.linspace(200.0, 100.0, 300),
                     np.linspace(100.0, 260.0, 300)]
        nav = pd.Series(base, index=SESSIONS[:900])
        benchmark = pd.Series(np.linspace(100.0, 250.0, 900), index=SESSIONS[:900])
        result = f1_criteria(nav=nav, benchmark=benchmark,
                             trades=self.book([10.0] * 30,
                                              [2009 + i % 6 for i in range(30)]))
        assert not result["checks"]["drawdown_within_5pp_of_benchmark"]
