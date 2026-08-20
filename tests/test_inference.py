"""M1／M2／M5 推論修正層的單元測試（合成 fixtures）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.inference import (
    MIN_TRADEABLE_EDGE,
    SensitivityResult,
    block_length_sensitivity,
    economic_significance,
    label_verdict,
    multiple_testing_budget,
    stationary_bootstrap_ci,
)

SESSIONS = pd.bdate_range("2015-01-01", periods=1200)


def synthetic_events(effect: float, *, n: int = 2000, noise: float = 0.05,
                     seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    positions = rng.integers(0, len(SESSIONS) - 200, size=n)
    return pd.DataFrame({
        "stock_id": [f"{1000 + i % 300}" for i in range(n)],
        "event_date": SESSIONS[positions],
        "car_60": effect + rng.normal(0, noise, size=n),
    })


# 十個「市況」水準：平均 +1.0%，但彼此差異遠大於平均值本身。
# 刻意寫死而不隨機抽，否則這個示範會隨 seed 時靈時不靈。
CLUSTER_LEVELS = (0.05, -0.03, 0.04, -0.02, 0.03, -0.01, 0.02, -0.02, 0.01, 0.03)
CLUSTER_SPACING = 100
CLUSTER_SPAN = 40


def clustered_events(*, seed: int = 11) -> pd.DataFrame:
    """報酬由少數幾個時間叢集主導——block 長度在這裡會直接改變判決。

    每 100 個交易日一個叢集，事件落在該叢集的前 40 個交易日內，
    同一叢集的事件共用一個市況水準。因此真正的獨立單位是**10 個叢集**，
    不是 2,000 個事件。
    """
    rng = np.random.default_rng(seed)
    rows = []
    for index, level in enumerate(CLUSTER_LEVELS):
        start = index * CLUSTER_SPACING
        for _ in range(200):
            rows.append({
                "stock_id": f"{2000 + rng.integers(0, 200)}",
                "event_date": SESSIONS[start + rng.integers(0, CLUSTER_SPAN)],
                "car_60": level + rng.normal(0, 0.01),
            })
    return pd.DataFrame(rows)


class TestBlockLengthSensitivity:
    def test_every_requested_block_length_is_reported(self):
        result = block_length_sensitivity(
            synthetic_events(0.0), 60, sessions=SESSIONS,
            blocks=(5, 20, 60), draws=200)
        assert sorted(result.by_block) == [5, 20, 60]
        assert all("p_value" in v for v in result.by_block.values())

    def test_a_strong_uniform_effect_is_stable_across_block_lengths(self):
        """效果強且時間上均勻時，block 長度不該改變判決。"""
        result = block_length_sensitivity(
            synthetic_events(0.05, noise=0.02), 60, sessions=SESSIONS,
            blocks=(5, 20, 60, 120), draws=300)
        assert result.verdict_is_stable()
        assert all(p < 0.05 for p in result.p_values)

    def test_time_clustered_data_flips_the_verdict_with_block_length(self):
        """M1 的核心：同一組資料，block 5 顯著、block 100 不顯著。

        短 block 把同一叢集內高度相關的事件當成獨立樣本，嚴重低估標準誤。
        block 100 剛好對齊真正的獨立單位（10 個叢集），此時效果消失。
        這正是「20 日 block 檢定 120 日累積報酬」會出的問題。
        """
        events = clustered_events()
        result = block_length_sensitivity(
            events, 60, sessions=SESSIONS, blocks=(5, CLUSTER_SPACING), draws=500)
        assert result.by_block[5]["p_value"] < 0.05
        assert result.by_block[CLUSTER_SPACING]["p_value"] > 0.05
        assert not result.verdict_is_stable()

    def test_the_confidence_interval_widens_as_the_block_grows(self):
        """即使判決沒翻，區間寬度也會誠實反映 block 的假設強度。"""
        events = clustered_events()
        result = block_length_sensitivity(
            events, 60, sessions=SESSIONS, blocks=(5, CLUSTER_SPACING), draws=500)
        narrow = result.by_block[5]
        wide = result.by_block[CLUSTER_SPACING]
        assert (wide["ci_high"] - wide["ci_low"]) > (narrow["ci_high"] - narrow["ci_low"])

    def test_verdict_is_stable_is_false_when_p_values_straddle_alpha(self):
        result = SensitivityResult(window=60, by_block={
            5: {"p_value": 0.01}, 120: {"p_value": 0.44}})
        assert not result.verdict_is_stable()
        assert result.p_range == pytest.approx(0.43)


class TestStationaryBootstrap:
    def test_is_deterministic_for_a_fixed_seed(self):
        events = synthetic_events(0.01)
        a = stationary_bootstrap_ci(events, 60, sessions=SESSIONS, draws=100)
        b = stationary_bootstrap_ci(events, 60, sessions=SESSIONS, draws=100)
        assert a == b

    def test_confidence_interval_brackets_the_sample_mean(self):
        events = synthetic_events(0.02)
        low, high, _ = stationary_bootstrap_ci(
            events, 60, sessions=SESSIONS, draws=300)
        assert low < events["car_60"].mean() < high

    def test_a_zero_effect_is_not_significant(self):
        _, _, p = stationary_bootstrap_ci(
            synthetic_events(0.0), 60, sessions=SESSIONS, draws=400)
        assert p > 0.05

    def test_empty_input_returns_nan_rather_than_raising(self):
        empty = pd.DataFrame({"stock_id": [], "event_date": [], "car_60": []})
        assert all(np.isnan(v) for v in
                   stationary_bootstrap_ci(empty, 60, sessions=SESSIONS, draws=10))

    def test_event_dates_outside_the_calendar_are_rejected(self):
        events = synthetic_events(0.0, n=10)
        events.loc[0, "event_date"] = pd.Timestamp("1990-01-01")
        with pytest.raises(ValueError, match="交易日曆"):
            stationary_bootstrap_ci(events, 60, sessions=SESSIONS, draws=10)


class TestEconomicSignificance:
    def test_an_edge_below_the_cost_threshold_fails_even_when_precise(self):
        """M5 的核心：+0.5% 就算量得再準，也蓋不過 1.185% 的換手成本。"""
        result = economic_significance(
            synthetic_events(0.005, noise=0.01), 60,
            sessions=SESSIONS, draws=300)
        assert result["observed"] == pytest.approx(0.005, abs=1e-3)
        assert not result["exceeds_threshold"]
        assert result["p_value"] > 0.05

    def test_an_edge_clearly_above_the_threshold_passes(self):
        result = economic_significance(
            synthetic_events(0.08, noise=0.02), 60,
            sessions=SESSIONS, draws=300)
        assert result["exceeds_threshold"]
        assert result["p_value"] < 0.05

    def test_the_default_threshold_is_cost_plus_margin_not_zero(self):
        assert MIN_TRADEABLE_EDGE > 0.02
        result = economic_significance(
            synthetic_events(0.0), 60, sessions=SESSIONS, draws=100)
        assert result["threshold"] == pytest.approx(MIN_TRADEABLE_EDGE)


class TestMultipleTestingBudget:
    def test_thirty_six_cells_expect_almost_two_false_positives(self):
        budget = multiple_testing_budget(cells=36, significant=1)
        assert budget["expected_false_positives"] == pytest.approx(1.8)
        assert budget["indistinguishable_from_noise"]

    def test_many_significant_cells_are_not_explained_by_noise(self):
        assert not multiple_testing_budget(
            cells=36, significant=9)["indistinguishable_from_noise"]

    def test_adjusted_alphas_are_tighter_than_the_nominal_one(self):
        budget = multiple_testing_budget(cells=36, significant=0)
        assert budget["bonferroni_alpha"] < budget["sidak_alpha"] < 0.05

    def test_zero_cells_is_rejected_rather_than_silently_accepted(self):
        with pytest.raises(ValueError):
            multiple_testing_budget(cells=0, significant=0)


class TestVerdictLabel:
    def test_an_effect_below_the_economic_threshold_is_rejected(self):
        assert label_verdict(p_value=0.001, observed=0.004) == "rejected"

    def test_above_threshold_but_imprecise_is_suggestive_not_rejected(self):
        assert label_verdict(p_value=0.19, observed=0.028) == "suggestive"

    def test_above_threshold_and_significant_is_only_not_falsified(self):
        """刻意沒有「有效」這個標籤——確認要走完整驗收流程。"""
        assert label_verdict(p_value=0.01, observed=0.05) == "not_falsified"

    def test_missing_inputs_are_undetermined(self):
        assert label_verdict(p_value=float("nan"), observed=0.05) == "undetermined"
