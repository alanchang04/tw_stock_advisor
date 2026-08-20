"""Always-valid 推論的單元測試。

最重要的一條是覆蓋率測試：confidence sequence 的賣點就是
「整條路徑同時成立」，若它其實不成立，那 forward journal 每月監看
就會變成偷看 p 值。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.sequential import (
    SequentialPlan,
    confidence_sequence,
    months_for_expected_t,
    months_for_power,
    planning_horizon,
)

PLAN = SequentialPlan(sigma=12.55, alpha=0.05, tightest_at=60)


class TestConfidenceSequence:
    def test_the_interval_narrows_as_observations_accumulate(self):
        rng = np.random.default_rng(1)
        frame = confidence_sequence(rng.normal(0, 12.55, size=200), PLAN)
        assert frame["radius"].iloc[-1] < frame["radius"].iloc[9]

    def test_it_is_wider_than_the_fixed_sample_interval_at_the_same_n(self):
        """這就是『可以一直看』所付的代價，必須真的存在。"""
        rng = np.random.default_rng(2)
        values = rng.normal(0, 12.55, size=120)
        frame = confidence_sequence(values, PLAN)
        for n in (20, 60, 120):
            fixed = 1.96 * PLAN.sigma / np.sqrt(n)
            assert frame["radius"].iloc[n - 1] > fixed

    def test_the_running_mean_is_a_plain_cumulative_mean(self):
        values = [1.0, 3.0, 5.0, 7.0]
        frame = confidence_sequence(values, PLAN)
        assert frame["mean"].tolist() == pytest.approx([1.0, 2.0, 3.0, 4.0])

    def test_a_large_true_effect_is_detected_and_flagged(self):
        rng = np.random.default_rng(3)
        frame = confidence_sequence(rng.normal(8.0, 12.55, size=300), PLAN)
        assert frame["excludes_zero"].any()
        assert frame["lower"].iloc[-1] > 0

    def test_an_empty_input_returns_an_empty_frame_rather_than_raising(self):
        assert confidence_sequence([], PLAN).empty


class TestCoverageGuarantee:
    def test_the_error_rate_over_the_whole_path_stays_within_alpha(self):
        """核心保證：在 H0 為真時，**整條路徑**曾經排除 0 的機率 <= alpha。

        這正是固定樣本 p 值做不到的事——若改用 1.96/sqrt(n)，
        同樣的模擬會有遠高於 5% 的路徑在某一期「顯著」。
        """
        rng = np.random.default_rng(20260814)
        paths, length = 400, 150
        crossed = 0
        for _ in range(paths):
            values = rng.normal(0.0, PLAN.sigma, size=length)
            if confidence_sequence(values, PLAN)["excludes_zero"].any():
                crossed += 1
        assert crossed / paths <= PLAN.alpha

    def test_a_fixed_sample_rule_would_have_failed_the_same_test(self):
        """對照組：說明上面那條測的不是一個空的性質。"""
        rng = np.random.default_rng(20260814)
        paths, length = 400, 150
        n = np.arange(1, length + 1)
        crossed = 0
        for _ in range(paths):
            values = rng.normal(0.0, PLAN.sigma, size=length)
            mean = values.cumsum() / n
            radius = 1.96 * PLAN.sigma / np.sqrt(n)
            if np.any(np.abs(mean[9:]) > radius[9:]):      # 從第 10 期起每月偷看
                crossed += 1
        assert crossed / paths > PLAN.alpha


class TestPlanning:
    def test_expected_t_matches_the_closed_form(self):
        # (sigma * t / effect)^2
        assert months_for_expected_t(1.864, 12.55, 1.96) == 175

    def test_power_requires_more_periods_than_expected_t(self):
        expected = months_for_expected_t(1.864, 12.55, 1.96)
        powered = months_for_power(1.864, 12.55, power=0.80)
        assert powered > expected

    def test_one_sided_needs_fewer_periods_than_two_sided(self):
        assert (months_for_power(1.864, 12.55, one_sided=True)
                < months_for_power(1.864, 12.55))

    def test_required_periods_scale_with_the_inverse_square_of_the_effect(self):
        """效果減半 → 期數變四倍。這是為什麼「等」不是一個好計畫。"""
        big = months_for_expected_t(2.0, 12.55)
        small = months_for_expected_t(1.0, 12.55)
        assert small / big == pytest.approx(4.0, rel=0.01)

    def test_a_zero_effect_is_reported_as_unreachable(self):
        assert months_for_expected_t(0.0, 12.55) == -1

    def test_the_planning_block_is_labelled_as_not_evidence(self):
        assert "not evidence" in planning_horizon(1.864, 12.55)["note"]
