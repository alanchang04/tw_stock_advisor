"""Phase 3 橫斷面排序檢定的單元測試（合成 fixtures）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.cross_section import (
    MIN_CANDIDATES_PER_DATE,
    cross_sectional_rank,
    equal_weight_score,
    monotonicity,
    quantile_profile,
    random_selection_baseline,
    rank_ic,
    top_k_selection_return,
)


def frame_for(dates, factor, target=None):
    data = {"event_date": pd.to_datetime(dates), "f": factor}
    if target is not None:
        data["y"] = target
    return pd.DataFrame(data)


def test_rank_is_computed_within_each_date_not_across_dates():
    """跨日排名會把不同市況混在一起；必須逐日。"""
    frame = frame_for(["2020-01-02"] * 5 + ["2020-01-03"] * 5,
                      [1, 2, 3, 4, 5] + [10, 20, 30, 40, 50])
    ranks = cross_sectional_rank(frame, "f")
    assert ranks.iloc[4] == pytest.approx(1.0)      # 第一天的最大值
    assert ranks.iloc[9] == pytest.approx(1.0)      # 第二天的最大值也是 1.0


def test_dates_with_too_few_candidates_yield_nan():
    """兩三檔的百分位沒有意義，硬算只會製造雜訊。"""
    frame = frame_for(["2020-01-02"] * (MIN_CANDIDATES_PER_DATE - 1), [1, 2, 3, 4][:MIN_CANDIDATES_PER_DATE - 1])
    assert cross_sectional_rank(frame, "f").isna().all()


def test_quantile_profile_orders_buckets_ascending():
    n = 500
    rank = np.linspace(0, 1, n)
    frame = pd.DataFrame({"r": rank, "y": rank * 10})
    profile = quantile_profile(frame, "r", "y")
    assert list(profile["quantile"]) == [1, 2, 3, 4, 5]
    assert profile["mean"].is_monotonic_increasing


def test_monotonicity_detects_a_clean_ladder():
    profile = pd.DataFrame({"quantile": [1, 2, 3, 4, 5],
                            "mean": [-0.2, 0.3, 0.7, 1.2, 1.8]})
    result = monotonicity(profile)
    assert result["spearman"] == pytest.approx(1.0)
    assert result["strictly_monotonic"]
    assert result["top_minus_bottom"] == pytest.approx(2.0)


def test_monotonicity_rejects_a_lone_spike():
    """孤峰不是單調——PIPELINE §5 要求看到孤峰就丟掉整個因子。"""
    profile = pd.DataFrame({"quantile": [1, 2, 3, 4, 5],
                            "mean": [0.1, 0.2, 2.8, 0.1, 0.2]})
    assert not monotonicity(profile)["strictly_monotonic"]


def test_rank_ic_is_computed_per_date_then_averaged():
    """混在一起算會讓事件多的日子支配結果。"""
    dates = ["2020-01-02"] * 10 + ["2020-01-03"] * 10
    factor = list(range(10)) + list(range(10))
    target = list(range(10)) + list(reversed(range(10)))   # 一天正相關、一天負相關
    result = rank_ic(frame_for(dates, factor, target), "f", "y")
    assert result["dates"] == 2
    assert result["mean_ic"] == pytest.approx(0.0, abs=1e-9)


def test_rank_ic_reports_icir():
    dates = sum([[f"2020-01-{d:02d}"] * 10 for d in range(2, 12)], [])
    factor = list(range(10)) * 10
    target = list(range(10)) * 10
    result = rank_ic(frame_for(dates, factor, target), "f", "y")
    assert result["mean_ic"] == pytest.approx(1.0)
    assert result["dates"] == 10


def test_equal_weight_score_averages_available_ranks():
    frame = pd.DataFrame({"a": [0.2, 0.8], "b": [0.4, 1.0]})
    score = equal_weight_score(frame, ["a", "b"])
    assert score.iloc[0] == pytest.approx(0.3)
    assert score.iloc[1] == pytest.approx(0.9)


def test_equal_weight_score_needs_half_the_factors_present():
    frame = pd.DataFrame({"a": [np.nan, 0.8], "b": [np.nan, np.nan],
                          "c": [np.nan, 0.6], "d": [np.nan, np.nan]})
    score = equal_weight_score(frame, ["a", "b", "c", "d"])
    assert np.isnan(score.iloc[0])
    assert score.iloc[1] == pytest.approx(0.7)


def test_top_k_picks_the_highest_scores_each_date():
    frame = pd.DataFrame({
        "event_date": pd.to_datetime(["2020-01-02"] * 4),
        "s": [0.1, 0.9, 0.5, 0.7],
        "y": [-1.0, 10.0, 0.0, 5.0],
    })
    result = top_k_selection_return(frame, "s", "y", k=2)
    assert result.iloc[0] == pytest.approx(7.5)      # 取 0.9 與 0.7 → (10+5)/2


def test_random_baseline_is_deterministic_and_brackets_the_mean():
    frame = pd.DataFrame({
        "event_date": pd.to_datetime(["2020-01-02"] * 20),
        "y": np.linspace(-5, 5, 20),
    })
    a = random_selection_baseline(frame, "y", k=10, draws=200, seed=3)
    b = random_selection_baseline(frame, "y", k=10, draws=200, seed=3)
    assert a == b
    assert a["p05"] <= a["mean"] <= a["p95"]
