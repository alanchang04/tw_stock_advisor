"""M15~M17 前視守門與時機指標的單元測試（合成 fixtures）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.policy import (
    assert_information_precedes_decision,
    exposure_summary,
    landmark_confirmation,
    max_adverse_excursion,
    restricted_mean_time,
    time_to_breakeven,
    wait_for_trigger_entry,
)

SESSIONS = pd.bdate_range("2020-01-01", periods=200)
STOCKS = ["1101", "2330", "2454"]


def zeros() -> pd.DataFrame:
    return pd.DataFrame(0.0, index=SESSIONS, columns=STOCKS)


def falses() -> pd.DataFrame:
    return pd.DataFrame(False, index=SESSIONS, columns=STOCKS)


def events_at(pairs) -> pd.DataFrame:
    return pd.DataFrame({
        "stock_id": [s for s, _ in pairs],
        "event_date": [SESSIONS[i] for _, i in pairs],
    })


class TestLandmarkConfirmation:
    def test_decision_date_is_pushed_to_the_end_of_the_confirmation_window(self):
        """M15 的核心：確認要 20 天才知道，決策就不能停在第 0 天。"""
        result = landmark_confirmation(
            events_at([("2330", 10)]), zeros(),
            sessions=SESSIONS, window_sessions=20)
        assert result.loc[0, "information_end"] == SESSIONS[30]
        assert result.loc[0, "decision_date"] == SESSIONS[30]

    def test_activity_inside_the_window_is_summed(self):
        flags = zeros()
        flags.iloc[11:16, flags.columns.get_loc("2330")] = 30.0   # 5 天各 30
        result = landmark_confirmation(
            events_at([("2330", 10)]), flags,
            sessions=SESSIONS, window_sessions=20, threshold=100.0)
        assert result.loc[0, "confirmation_value"] == pytest.approx(150.0)
        assert bool(result.loc[0, "confirmed"])

    def test_activity_on_the_event_day_itself_is_excluded(self):
        """事件日當天的買超屬於事件前既有的資訊，不算入「事後確認」。"""
        flags = zeros()
        flags.iloc[10, flags.columns.get_loc("2330")] = 500.0
        result = landmark_confirmation(
            events_at([("2330", 10)]), flags,
            sessions=SESSIONS, window_sessions=20, threshold=100.0)
        assert result.loc[0, "confirmation_value"] == pytest.approx(0.0)
        assert not bool(result.loc[0, "confirmed"])

    def test_activity_after_the_window_does_not_count(self):
        flags = zeros()
        flags.iloc[31, flags.columns.get_loc("2330")] = 500.0
        result = landmark_confirmation(
            events_at([("2330", 10)]), flags,
            sessions=SESSIONS, window_sessions=20, threshold=100.0)
        assert not bool(result.loc[0, "confirmed"])

    def test_a_window_running_past_the_calendar_yields_no_decision(self):
        result = landmark_confirmation(
            events_at([("2330", 195)]), zeros(),
            sessions=SESSIONS, window_sessions=20)
        assert pd.isna(result.loc[0, "decision_date"])

    def test_a_zero_window_is_rejected(self):
        with pytest.raises(ValueError):
            landmark_confirmation(events_at([("2330", 10)]), zeros(),
                                  sessions=SESSIONS, window_sessions=0)


class TestInformationOrderingGuard:
    def test_forward_return_starting_before_information_ends_is_rejected(self):
        """這正是 H12 會虛高的那個組合：用那 20 天分組又把它算進報酬。"""
        frame = pd.DataFrame({
            "information_end": [SESSIONS[30]],
            "forward_start": [SESSIONS[11]],
        })
        with pytest.raises(ValueError, match="延遲資訊前視"):
            assert_information_precedes_decision(frame)

    def test_forward_return_starting_on_the_information_day_is_also_rejected(self):
        frame = pd.DataFrame({"information_end": [SESSIONS[30]],
                              "forward_start": [SESSIONS[30]]})
        with pytest.raises(ValueError):
            assert_information_precedes_decision(frame)

    def test_a_correctly_ordered_frame_passes(self):
        frame = pd.DataFrame({"information_end": [SESSIONS[30]],
                              "forward_start": [SESSIONS[31]]})
        assert_information_precedes_decision(frame) is None

    def test_missing_columns_are_rejected_rather_than_skipped(self):
        with pytest.raises(ValueError):
            assert_information_precedes_decision(pd.DataFrame({"a": [1]}))


class TestWaitForTriggerEntry:
    def test_entry_happens_the_session_after_the_trigger(self):
        trigger = falses()
        trigger.iloc[35, trigger.columns.get_loc("2330")] = True
        result = wait_for_trigger_entry(
            pd.DataFrame({"stock_id": ["2330"], "decision_date": [SESSIONS[30]]}),
            trigger, sessions=SESSIONS, max_wait_sessions=20)
        assert bool(result.loc[0, "entered"])
        assert result.loc[0, "entry_date"] == SESSIONS[36]
        assert result.loc[0, "wait_sessions"] == pytest.approx(5.0)

    def test_candidates_that_never_trigger_are_kept_not_dropped(self):
        """M16 的核心：沒觸發的必須留在結果裡，否則就是用未來事件篩樣本。"""
        result = wait_for_trigger_entry(
            pd.DataFrame({"stock_id": ["2330"], "decision_date": [SESSIONS[30]]}),
            falses(), sessions=SESSIONS, max_wait_sessions=20)
        assert len(result) == 1
        assert not bool(result.loc[0, "entered"])
        assert pd.isna(result.loc[0, "entry_date"])

    def test_a_trigger_after_the_wait_limit_is_not_taken(self):
        trigger = falses()
        trigger.iloc[55, trigger.columns.get_loc("2330")] = True
        result = wait_for_trigger_entry(
            pd.DataFrame({"stock_id": ["2330"], "decision_date": [SESSIONS[30]]}),
            trigger, sessions=SESSIONS, max_wait_sessions=20)
        assert not bool(result.loc[0, "entered"])

    def test_a_trigger_on_the_decision_day_itself_is_not_used(self):
        """決策日收盤後才做決定，當天的觸發屬於決策所用的資訊。"""
        trigger = falses()
        trigger.iloc[30, trigger.columns.get_loc("2330")] = True
        result = wait_for_trigger_entry(
            pd.DataFrame({"stock_id": ["2330"], "decision_date": [SESSIONS[30]]}),
            trigger, sessions=SESSIONS, max_wait_sessions=20)
        assert not bool(result.loc[0, "entered"])

    def test_a_non_positive_wait_limit_is_rejected(self):
        with pytest.raises(ValueError):
            wait_for_trigger_entry(
                pd.DataFrame({"stock_id": ["2330"],
                              "decision_date": [SESSIONS[30]]}),
                falses(), sessions=SESSIONS, max_wait_sessions=0)


class TestExposureSummary:
    def test_participation_rate_counts_the_ones_that_never_entered(self):
        policy = pd.DataFrame({"entered": [True, True, False, False, False],
                               "wait_sessions": [3.0, 7.0, np.nan, np.nan, np.nan]})
        summary = exposure_summary(policy)
        assert summary["participation_rate"] == pytest.approx(0.4)
        assert summary["mean_wait_sessions"] == pytest.approx(5.0)

    def test_a_policy_that_never_enters_is_visible_as_zero_participation(self):
        """M17：不報這個數字的話，「永遠不買」會是 MAE 最好的策略。"""
        policy = pd.DataFrame({"entered": [False] * 4,
                               "wait_sessions": [np.nan] * 4})
        assert exposure_summary(policy)["participation_rate"] == pytest.approx(0.0)


class TestTimingMetrics:
    @staticmethod
    def price_frames():
        opens = pd.DataFrame(100.0, index=SESSIONS, columns=STOCKS)
        lows = pd.DataFrame(100.0, index=SESSIONS, columns=STOCKS)
        closes = pd.DataFrame(100.0, index=SESSIONS, columns=STOCKS)
        return opens, lows, closes

    def test_mae_uses_the_intraday_low_not_the_close(self):
        opens, lows, closes = self.price_frames()
        column = lows.columns.get_loc("2330")
        lows.iloc[12, column] = 85.0          # 盤中破底但收盤沒有
        entries = pd.DataFrame({"stock_id": ["2330"], "entry_date": [SESSIONS[10]]})
        mae = max_adverse_excursion(entries, opens, lows,
                                    sessions=SESSIONS, horizon=20)
        assert mae.iloc[0] == pytest.approx(-0.15)

    def test_mae_is_zero_when_price_never_falls_below_entry(self):
        opens, lows, closes = self.price_frames()
        entries = pd.DataFrame({"stock_id": ["2330"], "entry_date": [SESSIONS[10]]})
        assert max_adverse_excursion(entries, opens, lows,
                                     sessions=SESSIONS, horizon=20).iloc[0] == 0.0

    def test_time_to_breakeven_uses_the_cost_threshold_not_a_chosen_number(self):
        opens, lows, closes = self.price_frames()
        column = closes.columns.get_loc("2330")
        closes.iloc[13, column] = 102.0        # 第 4 天達 +2% > 1.185%
        entries = pd.DataFrame({"stock_id": ["2330"], "entry_date": [SESSIONS[10]]})
        result = time_to_breakeven(entries, opens, closes,
                                   sessions=SESSIONS, horizon=20)
        assert result.loc[0, "duration"] == pytest.approx(4.0)
        assert result.loc[0, "event"] == 1.0

    def test_never_reaching_breakeven_is_censored_not_dropped(self):
        opens, lows, closes = self.price_frames()
        entries = pd.DataFrame({"stock_id": ["2330"], "entry_date": [SESSIONS[10]]})
        result = time_to_breakeven(entries, opens, closes,
                                   sessions=SESSIONS, horizon=20)
        assert result.loc[0, "duration"] == pytest.approx(20.0)
        assert result.loc[0, "event"] == 0.0

    def test_restricted_mean_caps_at_the_horizon(self):
        durations = pd.DataFrame({"duration": [4.0, 20.0, 20.0], "event": [1, 0, 0]})
        assert restricted_mean_time(durations, horizon=20) == pytest.approx(44 / 3)
