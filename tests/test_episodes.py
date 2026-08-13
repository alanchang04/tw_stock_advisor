"""M3／M4 事件叢集去重與右尾稽核的單元測試（合成 fixtures）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.episodes import (
    deduplicate_overlapping_events,
    leave_one_out_audit,
    tail_breadth,
)

SESSIONS = pd.bdate_range("2015-01-01", periods=800)


def events_at(pairs) -> pd.DataFrame:
    return pd.DataFrame({
        "stock_id": [s for s, _ in pairs],
        "event_date": [SESSIONS[i] for _, i in pairs],
    })


class TestDeduplication:
    def test_repeat_triggers_inside_the_window_collapse_to_one_episode(self):
        """M3 的核心情境：一檔股票一波行情內觸發四次，只能算一次。"""
        result = deduplicate_overlapping_events(
            events_at([("2330", 0), ("2330", 9), ("2330", 22), ("2330", 41)]),
            sessions=SESSIONS, window_sessions=60)
        assert len(result) == 1
        assert result.loc[0, "event_date"] == SESSIONS[0]
        assert result.loc[0, "absorbed"] == 3

    def test_triggers_beyond_the_window_stay_independent(self):
        result = deduplicate_overlapping_events(
            events_at([("2330", 0), ("2330", 60), ("2330", 120)]),
            sessions=SESSIONS, window_sessions=60)
        assert len(result) == 3
        assert list(result["absorbed"]) == [0, 0, 0]
        assert list(result["episode_id"]) == ["2330#1", "2330#2", "2330#3"]

    def test_the_boundary_is_inclusive_at_exactly_window_sessions(self):
        """恰好隔 window 個交易日＝窗剛好接續、不重疊 → 保留。"""
        kept = deduplicate_overlapping_events(
            events_at([("2330", 0), ("2330", 20)]),
            sessions=SESSIONS, window_sessions=20)
        merged = deduplicate_overlapping_events(
            events_at([("2330", 0), ("2330", 19)]),
            sessions=SESSIONS, window_sessions=20)
        assert len(kept) == 2
        assert len(merged) == 1

    def test_different_stocks_are_never_merged(self):
        result = deduplicate_overlapping_events(
            events_at([("2330", 0), ("2317", 1), ("2454", 2)]),
            sessions=SESSIONS, window_sessions=60)
        assert len(result) == 3

    def test_the_chain_restarts_from_the_kept_event_not_the_absorbed_one(self):
        """0、50、100：50 被 0 吸收後，100 與 0 相隔 100 >= 60，必須保留。

        若錯誤地以「最後一次觸發」當基準，100 會被 50 吸收而消失。
        """
        result = deduplicate_overlapping_events(
            events_at([("2330", 0), ("2330", 50), ("2330", 100)]),
            sessions=SESSIONS, window_sessions=60)
        assert list(result["event_date"]) == [SESSIONS[0], SESSIONS[100]]

    def test_extra_columns_survive_deduplication(self):
        frame = events_at([("2330", 0), ("2330", 5)]).assign(car_60=[0.5, 0.4])
        result = deduplicate_overlapping_events(
            frame, sessions=SESSIONS, window_sessions=60)
        assert result.loc[0, "car_60"] == pytest.approx(0.5)

    def test_a_non_positive_window_is_rejected(self):
        with pytest.raises(ValueError):
            deduplicate_overlapping_events(
                events_at([("2330", 0)]), sessions=SESSIONS, window_sessions=0)

    def test_dates_outside_the_calendar_are_rejected(self):
        frame = events_at([("2330", 0)])
        frame.loc[0, "event_date"] = pd.Timestamp("1990-01-01")
        with pytest.raises(ValueError, match="交易日曆"):
            deduplicate_overlapping_events(
                frame, sessions=SESSIONS, window_sessions=60)


class TestTailBreadth:
    def test_a_tail_concentrated_in_one_stock_is_flagged(self):
        frame = pd.DataFrame({
            "stock_id": ["9999"] * 10 + [f"{1000 + i}" for i in range(190)],
            "event_date": [SESSIONS[i] for i in range(200)],
            "car_60": [5.0] * 10 + [0.0] * 190,
        })
        result = tail_breadth(frame, "car_60", top_fraction=0.05)
        assert result["tail_events"] == 10
        assert result["distinct_stocks"] == 1
        assert result["distinct_stock_ratio"] == pytest.approx(0.1)
        assert result["stock_hhi"] == pytest.approx(1.0)

    def test_a_broad_tail_scores_near_one(self):
        rng = np.random.default_rng(3)
        frame = pd.DataFrame({
            "stock_id": [f"{1000 + i}" for i in range(400)],
            "event_date": [SESSIONS[i % 800] for i in range(400)],
            "car_60": rng.normal(0, 1, size=400),
        })
        result = tail_breadth(frame, "car_60", top_fraction=0.05)
        assert result["distinct_stock_ratio"] == pytest.approx(1.0)
        assert result["stock_hhi"] < 0.1

    def test_tail_share_above_one_means_the_rest_is_negative(self):
        frame = pd.DataFrame({
            "stock_id": [f"{1000 + i}" for i in range(100)],
            "event_date": [SESSIONS[i] for i in range(100)],
            "car_60": [20.0] * 5 + [-0.1] * 95,
        })
        result = tail_breadth(frame, "car_60", top_fraction=0.05)
        assert result["tail_share_of_sum"] > 1.0

    def test_empty_input_reports_zero_rather_than_raising(self):
        empty = pd.DataFrame({"stock_id": [], "event_date": [], "car_60": []})
        assert tail_breadth(empty, "car_60")["tail_events"] == 0


class TestLeaveOneOut:
    def test_a_single_year_carrying_the_result_is_detected(self):
        rows = []
        for year, level in ((2018, -0.01), (2019, -0.01), (2020, 0.30)):
            for i in range(100):
                rows.append({"stock_id": f"{1000 + i}",
                             "event_date": pd.Timestamp(f"{year}-06-01"),
                             "car_60": level})
        result = leave_one_out_audit(pd.DataFrame(rows), "car_60", by="year")
        assert result["full_mean"] > 0
        assert result["most_influential_group"] == 2020
        assert result["mean_without_it"] < 0
        assert result["flips_sign_groups"] == 1
        assert not result["conclusion_survives_any_single_removal"]

    def test_a_broadly_supported_result_survives_every_removal(self):
        rows = [{"stock_id": f"{1000 + i}",
                 "event_date": pd.Timestamp(f"{year}-06-01"),
                 "car_60": 0.05}
                for year in (2018, 2019, 2020) for i in range(100)]
        result = leave_one_out_audit(pd.DataFrame(rows), "car_60", by="year")
        assert result["flips_sign_groups"] == 0
        assert result["conclusion_survives_any_single_removal"]

    def test_grouping_by_an_arbitrary_column_is_supported(self):
        frame = pd.DataFrame({
            "stock_id": ["A", "A", "B", "B"],
            "event_date": [SESSIONS[i] for i in range(4)],
            "car_60": [1.0, 1.0, -0.1, -0.1],
        })
        result = leave_one_out_audit(frame, "car_60", by="stock_id")
        assert result["most_influential_group"] == "A"
        assert result["mean_without_it"] == pytest.approx(-0.1)

    def test_an_unknown_grouping_column_is_rejected(self):
        frame = pd.DataFrame({"stock_id": ["A"], "event_date": [SESSIONS[0]],
                              "car_60": [1.0]})
        with pytest.raises(ValueError):
            leave_one_out_audit(frame, "car_60", by="sector")
