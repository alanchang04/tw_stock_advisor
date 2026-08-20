"""M7 月頻 Fama-MacBeth 與反應路徑的單元測試（合成 fixtures）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.fama_macbeth import (
    MIN_CROSS_SECTION,
    build_forward_panel,
    cumulative_from_path,
    fama_macbeth,
    long_only_excess,
    newey_west_se,
    response_path,
)


def monthly_panel(*, effect_by_horizon, periods: int = 130, width: int = 400,
                  noise: float = 0.02, seed: int = 5) -> pd.DataFrame:
    """每月 ``width`` 檔股票，一半有訊號；訊號組在各領先期多出指定的報酬。

    ``noise`` 刻意小於真實個股月報酬的離散度。這裡要檢定的是**估計式有沒有
    寫對**，不是它在真實雜訊下的功效——後者要用真資料量，不是合成 fixture。
    """
    rng = np.random.default_rng(seed)
    rows = []
    for t in range(periods):
        signal = np.zeros(width)
        signal[: width // 2] = 1.0
        record = {"period": pd.Period(f"2015-01", freq="M") + t,
                  "signal": signal}
        for horizon, effect in effect_by_horizon.items():
            record[f"fwd_{horizon}"] = (
                signal * effect + rng.normal(0, noise, size=width))
        rows.append(pd.DataFrame(record))
    return pd.concat(rows, ignore_index=True)


class TestNeweyWest:
    def test_zero_lags_reduces_to_the_plain_standard_error(self):
        series = pd.Series([0.1, -0.2, 0.3, 0.05, -0.1, 0.22])
        expected = float(np.std(series.to_numpy()) / np.sqrt(len(series)))
        assert newey_west_se(series, lags=0) == pytest.approx(expected)

    def test_positive_autocorrelation_inflates_the_standard_error(self):
        """這正是要 Newey-West 的理由：忽略自相關會低估不確定性。"""
        rng = np.random.default_rng(1)
        shocks = rng.normal(0, 1, size=400)
        persistent = pd.Series(shocks).rolling(10).mean().dropna()
        assert newey_west_se(persistent, lags=10) > newey_west_se(persistent, lags=0)

    def test_a_series_shorter_than_two_points_is_nan(self):
        assert np.isnan(newey_west_se(pd.Series([0.1]), lags=3))

    def test_lags_are_capped_at_the_series_length(self):
        assert np.isfinite(newey_west_se(pd.Series([0.1, 0.2, 0.3]), lags=99))

    def test_negative_lags_are_rejected(self):
        with pytest.raises(ValueError):
            newey_west_se(pd.Series([0.1, 0.2, 0.3]), lags=-1)


class TestFamaMacBeth:
    def test_a_binary_factor_recovers_the_group_mean_difference(self):
        """二元因子的 OLS 斜率＝有訊號組平均 − 無訊號組平均。

        係數可以直接讀成超額報酬，這是選單因子迴歸而非分組比較的理由。
        """
        panel = monthly_panel(effect_by_horizon={1: 0.004})
        result = fama_macbeth(panel, factor="signal", target="fwd_1")
        assert result["periods"] == 130
        assert result["mean"] == pytest.approx(0.004, abs=6e-4)
        assert result["p_value"] < 0.05

    def test_the_test_is_calibrated_under_the_null(self):
        """無效果時的誤報率應接近名目 5%，而不是某一個 seed 的運氣。

        單一 seed 的「不顯著」本來就有 5% 機率失敗，那種測試只是在賭。
        這裡跑 24 個獨立面板檢查拒絕率——這才是「p 值有沒有意義」的性質。
        """
        rejections = sum(
            fama_macbeth(monthly_panel(effect_by_horizon={1: 0.0}, periods=100,
                                       width=200, seed=seed),
                         factor="signal", target="fwd_1")["p_value"] < 0.05
            for seed in range(24)
        )
        # Binomial(24, 0.05) 下 P(X >= 5) < 0.01；超過就是估計式有偏
        assert rejections <= 4

    def test_the_cross_section_is_wide_where_the_information_lives(self):
        """M7 的重點：137 個月不是「137 個觀測」，每個月還有數百檔的寬度。"""
        panel = monthly_panel(effect_by_horizon={1: 0.004})
        result = fama_macbeth(panel, factor="signal", target="fwd_1")
        assert result["median_cross_section"] == 400

    def test_thin_months_are_dropped_rather_than_estimated_noisily(self):
        panel = monthly_panel(effect_by_horizon={1: 0.004}, periods=10)
        thin = panel[panel["period"] == panel["period"].min()].head(
            MIN_CROSS_SECTION - 1)
        rest = panel[panel["period"] != panel["period"].min()]
        result = fama_macbeth(pd.concat([thin, rest]), factor="signal",
                              target="fwd_1")
        assert result["periods"] == 9

    def test_a_month_where_every_stock_has_the_signal_is_dropped(self):
        """全體同值時斜率無定義，必須丟掉而不是回傳 0。"""
        panel = monthly_panel(effect_by_horizon={1: 0.004}, periods=10)
        first = panel["period"].min()
        panel.loc[panel["period"] == first, "signal"] = 1.0
        assert fama_macbeth(panel, factor="signal", target="fwd_1")["periods"] == 9

    def test_missing_columns_are_rejected(self):
        panel = monthly_panel(effect_by_horizon={1: 0.0})
        with pytest.raises(ValueError):
            fama_macbeth(panel, factor="signal", target="fwd_9")

    def test_the_coefficient_is_invariant_to_the_choice_of_benchmark(self):
        """逐期截距已吸收全體共有的報酬，所以換基準不會改變因子係數。

        這條性質省掉一整輪爭論：「該用 0050 還是等權 universe」只影響
        組合層級的績效宣稱，不影響橫斷面選股資訊的估計。
        """
        panel = monthly_panel(effect_by_horizon={1: 0.004})
        rng = np.random.default_rng(2)
        shift = dict(zip(panel["period"].unique(),
                         rng.normal(0, 0.05, size=panel["period"].nunique())))
        rebased = panel.assign(
            fwd_1=panel["fwd_1"] - panel["period"].map(shift))
        assert (fama_macbeth(rebased, factor="signal", target="fwd_1")["mean"]
                == pytest.approx(
                    fama_macbeth(panel, factor="signal", target="fwd_1")["mean"]))


class TestLongOnlyExcess:
    @staticmethod
    def panel_with_treated_fraction(fraction: float, spread: float,
                                    periods: int = 120, width: int = 400,
                                    seed: int = 12) -> pd.DataFrame:
        rng = np.random.default_rng(seed)
        treated = int(width * fraction)
        rows = []
        for t in range(periods):
            signal = np.zeros(width)
            signal[:treated] = 1.0
            rows.append(pd.DataFrame({
                "period": pd.Period("2015-01", freq="M") + t,
                "signal": signal,
                "fwd_1": signal * spread + rng.normal(0, 0.01, size=width),
            }))
        return pd.concat(rows, ignore_index=True)

    def test_long_only_equals_one_minus_p_times_the_spread(self):
        panel = self.panel_with_treated_fraction(0.25, 0.04)
        spread = fama_macbeth(panel, factor="signal", target="fwd_1")["mean"]
        long_only = long_only_excess(panel, factor="signal", target="fwd_1")
        assert long_only["treated_fraction"] == pytest.approx(0.25)
        assert long_only["mean"] == pytest.approx(0.75 * spread, rel=1e-3)

    def test_a_more_selective_signal_can_win_on_long_only_despite_a_smaller_spread(self):
        """這是本函式存在的理由：只看多空價差會把可部署性排反。"""
        broad = self.panel_with_treated_fraction(0.60, 0.030)     # 價差大、不選擇
        selective = self.panel_with_treated_fraction(0.20, 0.025)  # 價差小、很選擇
        assert (fama_macbeth(broad, factor="signal", target="fwd_1")["mean"]
                > fama_macbeth(selective, factor="signal", target="fwd_1")["mean"])
        assert (long_only_excess(broad, factor="signal", target="fwd_1")["mean"]
                < long_only_excess(selective, factor="signal", target="fwd_1")["mean"])

    def test_periods_where_everyone_or_nobody_has_the_signal_are_skipped(self):
        panel = self.panel_with_treated_fraction(0.25, 0.04, periods=10)
        first = panel["period"].min()
        panel.loc[panel["period"] == first, "signal"] = 0.0
        assert long_only_excess(panel, factor="signal",
                                target="fwd_1")["periods"] == 9

    def test_missing_columns_are_rejected(self):
        panel = self.panel_with_treated_fraction(0.25, 0.0, periods=5)
        with pytest.raises(ValueError):
            long_only_excess(panel, factor="signal", target="fwd_9")


class TestBuildForwardPanel:
    @staticmethod
    def wide_frames(periods: int = 8, stocks: int = 6):
        index = pd.period_range("2015-01", periods=periods, freq="M")
        columns = [f"{1000 + i}" for i in range(stocks)]
        rng = np.random.default_rng(4)
        returns = pd.DataFrame(rng.normal(0, 0.05, size=(periods, stocks)),
                               index=index, columns=columns)
        signal = pd.DataFrame(0.0, index=index, columns=columns)
        signal.iloc[:, :3] = 1.0
        # 後半段把最後兩檔判定為不合格（模擬流動性掉出門檻）
        eligible = returns.copy()
        eligible.iloc[periods // 2:, -2:] = np.nan
        return signal, eligible, returns

    def test_forward_returns_are_not_filtered_by_future_eligibility(self):
        """M13：把已遮罩的矩陣同時當成前瞻報酬，會少掉「後來不合格」的觀測。

        那些觀測消失才是存活者偏差的實體——它們多半是表現差的那些。
        """
        signal, eligible, returns = self.wide_frames()
        correct = build_forward_panel(
            signal=signal, decision_frame=eligible,
            forward_frame=returns, horizons=(1, 2))
        biased = build_forward_panel(
            signal=signal, decision_frame=eligible,
            forward_frame=eligible, horizons=(1, 2))
        assert correct["fwd_1"].notna().sum() > biased["fwd_1"].notna().sum()

    def test_the_cross_section_still_respects_decision_time_eligibility(self):
        """前瞻報酬不遮罩，但**進得了橫斷面的人**仍由決策時點決定。"""
        signal, eligible, returns = self.wide_frames()
        panel = build_forward_panel(
            signal=signal, decision_frame=eligible,
            forward_frame=returns, horizons=(1,))
        late = panel[panel["period"] >= pd.Period("2015-05", freq="M")]
        assert not late["stock_id"].isin(["1004", "1005"]).any()

    def test_controls_are_carried_through_unmasked(self):
        signal, eligible, returns = self.wide_frames()
        size = pd.DataFrame(1.5, index=returns.index, columns=returns.columns)
        panel = build_forward_panel(
            signal=signal, decision_frame=eligible, forward_frame=returns,
            controls={"log_size": size}, horizons=(1,))
        assert panel["log_size"].to_numpy() == pytest.approx(1.5)

    def test_every_requested_horizon_becomes_a_column(self):
        signal, eligible, returns = self.wide_frames()
        panel = build_forward_panel(
            signal=signal, decision_frame=eligible,
            forward_frame=returns, horizons=(1, 3, 6))
        assert {"fwd_1", "fwd_3", "fwd_6"} <= set(panel.columns)


class TestControls:
    @staticmethod
    def confounded_panel(periods: int = 130, width: int = 400,
                         seed: int = 9) -> pd.DataFrame:
        """訊號本身沒有效果，但它偏好大型股，而大型股當期報酬較高。

        不控制規模就會把規模溢酬誤讀成訊號效力——這正是 M9 在橫斷面設定下
        的實質內容（換基準沒有用，逐期截距早就把共同成分吸走了）。
        """
        rng = np.random.default_rng(seed)
        rows = []
        for t in range(periods):
            size = rng.normal(0, 1, size=width)
            signal = (size > 0).astype(float)      # 訊號與規模高度相關
            rows.append(pd.DataFrame({
                "period": pd.Period("2015-01", freq="M") + t,
                "signal": signal,
                "log_size": size,
                "fwd_1": 0.01 * size + rng.normal(0, 0.02, size=width),
            }))
        return pd.concat(rows, ignore_index=True)

    def test_an_uncontrolled_regression_reports_a_spurious_effect(self):
        result = fama_macbeth(self.confounded_panel(), factor="signal",
                              target="fwd_1")
        assert result["mean"] > 0.01
        assert result["p_value"] < 0.001

    def test_controlling_for_the_confound_removes_the_spurious_effect(self):
        result = fama_macbeth(self.confounded_panel(), factor="signal",
                              target="fwd_1", controls=["log_size"])
        assert abs(result["mean"]) < 0.002
        assert result["p_value"] > 0.05

    def test_a_control_collinear_with_the_factor_yields_no_coefficient(self):
        """完全共線時必須回報無定義，不能吐一個看似有意義的數字。"""
        panel = monthly_panel(effect_by_horizon={1: 0.004})
        panel["copy"] = panel["signal"]
        assert fama_macbeth(panel, factor="signal", target="fwd_1",
                            controls=["copy"])["periods"] == 0

    def test_a_missing_control_column_is_rejected(self):
        panel = monthly_panel(effect_by_horizon={1: 0.0})
        with pytest.raises(ValueError):
            fama_macbeth(panel, factor="signal", target="fwd_1",
                         controls=["log_size"])


class TestResponsePath:
    def test_the_path_recovers_each_horizon_separately(self):
        """診斷價值就在這裡：alpha 是逐月累積，還是只有某一期突然跳出來。"""
        panel = monthly_panel(
            effect_by_horizon={1: 0.006, 2: 0.004, 3: 0.002, 4: 0.0})
        path = response_path(panel, factor="signal", horizons=(1, 2, 3, 4))
        assert list(path["horizon_months"]) == [1, 2, 3, 4]
        assert path["mean"].to_numpy() == pytest.approx(
            [0.006, 0.004, 0.002, 0.0], abs=8e-4)

    def test_a_late_only_spike_is_visible_in_the_path(self):
        panel = monthly_panel(effect_by_horizon={1: 0.0, 2: 0.0, 3: 0.02})
        path = response_path(panel, factor="signal", horizons=(1, 2, 3))
        assert path.loc[2, "mean"] > 10 * abs(path.loc[0, "mean"])

    def test_a_missing_horizon_column_is_rejected_not_skipped(self):
        panel = monthly_panel(effect_by_horizon={1: 0.0})
        with pytest.raises(ValueError, match="反應路徑"):
            response_path(panel, factor="signal", horizons=(1, 2))


class TestCumulativeFromPath:
    def test_the_cumulative_equals_the_sum_of_the_path(self):
        panel = monthly_panel(effect_by_horizon={1: 0.006, 2: 0.004, 3: 0.002})
        path = response_path(panel, factor="signal", horizons=(1, 2, 3))
        cumulative = cumulative_from_path(panel, factor="signal",
                                          horizons=(1, 2, 3))
        assert cumulative["cumulative"] == pytest.approx(
            float(path["mean"].sum()), abs=1e-9)

    def test_the_standard_error_accounts_for_cross_horizon_covariance(self):
        """各期標準誤直接平方相加是錯的——那假設各期係數互相獨立。

        正確做法是先加總係數序列再算標準誤，因此兩者不會相等。
        """
        panel = monthly_panel(effect_by_horizon={1: 0.004, 2: 0.004, 3: 0.004})
        path = response_path(panel, factor="signal", horizons=(1, 2, 3))
        naive = float(np.sqrt((path["se"] ** 2).sum()))
        cumulative = cumulative_from_path(panel, factor="signal",
                                          horizons=(1, 2, 3))
        assert cumulative["se"] != pytest.approx(naive, rel=1e-6)

    def test_an_empty_panel_reports_nan_rather_than_raising(self):
        panel = monthly_panel(effect_by_horizon={1: 0.0}).iloc[0:0]
        result = cumulative_from_path(panel, factor="signal", horizons=(1,))
        assert result["periods"] == 0
        assert np.isnan(result["cumulative"])
