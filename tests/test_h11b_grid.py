"""H11b 的格點與前瞻對齊。

**這個檔存在的理由是一次具體的失敗。** H11b 第一版在建構期間報酬時多做了
一次 ``shift(-1)``，但 ``build_forward_panel`` 內部已經有 ``shift(-horizon)``，
於是整條反應路徑往後推了一期——``fwd_1`` 量到的其實是第二期。

它沒有讓任何東西壞掉，只是安靜地答錯問題。抓到它的是事前登記的預期
（「M+1 若小於單獨一段早窗，在算術上不可能」），不是測試。所以補上測試。
"""
from __future__ import annotations

import pandas as pd
import pytest

from research.fama_macbeth import build_forward_panel
from scripts.run_h11b_revenue_timing import execution_grid


def business_sessions(start: str, end: str) -> pd.DatetimeIndex:
    return pd.bdate_range(start, end)


class TestExecutionGrid:
    def test_each_revenue_month_maps_after_the_next_months_tenth(self):
        sessions = business_sessions("2020-01-01", "2020-12-31")
        periods = pd.PeriodIndex(["2020-01", "2020-02", "2020-03"], freq="M")
        grid = execution_grid(sessions, periods)

        assert len(grid) == 3
        # 2020-01 的營收 → 2/10 結束後第一個交易日 = 2/11（2/10 是週一，交易日）
        assert grid.loc[0, "execution"] == pd.Timestamp("2020-02-11")
        # 2020-04-10 是週五(耶穌受難日在台股不休)，故 4/13 週一
        assert grid.loc[2, "execution"] > pd.Timestamp("2020-04-10")

    def test_grid_is_strictly_increasing_and_one_session_per_period(self):
        sessions = business_sessions("2020-01-01", "2021-12-31")
        periods = pd.PeriodIndex(pd.period_range("2020-01", "2021-06", freq="M"))
        grid = execution_grid(sessions, periods)
        assert grid["execution"].is_monotonic_increasing
        assert not grid["execution"].duplicated().any()

    def test_periods_beyond_the_calendar_are_dropped_not_clamped(self):
        """日曆走不到的月份要消失，不能全部擠到最後一個交易日。"""
        sessions = business_sessions("2020-01-01", "2020-03-31")
        periods = pd.PeriodIndex(pd.period_range("2020-01", "2020-12", freq="M"))
        grid = execution_grid(sessions, periods)
        assert len(grid) < len(periods)
        assert not grid["execution"].duplicated().any()


class TestForwardAlignment:
    """``fwd_1`` 必須是「本格 → 下一格」，不是「下一格 → 再下一格」。"""

    # 每一格報酬刻意各不相同（+10%、+30%、+20%、+50%），差一期就看得出來。
    # 第一格的 pct_change 是 NaN，會被 decision_frame 濾掉——與正式腳本一致，
    # 因此斷言錨在第二格。
    DATES = pd.DatetimeIndex(["2020-02-11", "2020-03-11", "2020-04-13",
                              "2020-05-11", "2020-06-11"])
    PRICES = [100.0, 110.0, 143.0, 171.6, 257.4]
    ANCHOR = pd.Timestamp("2020-03-11")

    def frames(self):
        price = pd.DataFrame({"A": self.PRICES}, index=self.DATES)
        returns = price.pct_change(fill_method=None)
        signal = pd.DataFrame({"A": [1.0] * len(self.DATES)}, index=self.DATES)
        return returns, signal

    def build(self, forward=None):
        returns, signal = self.frames()
        return build_forward_panel(
            signal=signal, decision_frame=returns,
            forward_frame=returns if forward is None else forward,
            horizons=(1, 2)).set_index("period")

    def test_the_first_grid_point_is_dropped_because_it_has_no_return_yet(self):
        assert pd.Timestamp("2020-02-11") not in self.build().index

    def test_fwd_1_is_the_return_from_this_grid_point_to_the_next(self):
        # 2020-03-11 -> 2020-04-13 是 110 -> 143，也就是 +30%
        assert self.build().loc[self.ANCHOR, "fwd_1"] == pytest.approx(0.30)

    def test_fwd_2_is_one_further_along_and_distinct_from_fwd_1(self):
        # 2020-04-13 -> 2020-05-11 是 143 -> 171.6，也就是 +20%
        row = self.build().loc[self.ANCHOR]
        assert row["fwd_2"] == pytest.approx(0.20)
        assert row["fwd_1"] != pytest.approx(row["fwd_2"])

    def test_an_extra_shift_would_move_the_whole_path(self):
        """對照組：證明多一次 shift 會被上面的斷言抓到，而不是恰好相同。

        這正是 H11b 第一版犯的錯——它安靜地把 fwd_1 換成了第二期。
        """
        returns, _ = self.frames()
        bad = self.build(forward=returns.shift(-1)).loc[self.ANCHOR, "fwd_1"]
        assert bad == pytest.approx(0.20), "多一次 shift 會讓 fwd_1 變成第二期"
        assert bad != pytest.approx(0.30)
