"""族別切分：同一天對不同假說族可以有不同的污染狀態。

**這個檔存在的理由是舊設計的一個缺陷。** `CONTAMINATED` 原本是全域常數，
於是 2025-06 之後看起來對所有假說都不可用——但那個窗是現行波段策略的
調參目標窗，動能族從來沒有在那裡調過任何東西。

污染不是時間的屬性，是 (資料, 假說) 這一對的屬性。
"""
from __future__ import annotations

from datetime import date

import pytest

from research import data_splits as S


class TestContaminationIsPerFamily:
    def test_the_same_day_gets_different_answers_for_different_families(self):
        """2026-01-15：對波段是硬污染，對動能是封存。"""
        day = date(2026, 1, 15)
        assert S.family_split_of(day, "swing") == "contaminated"
        assert S.family_split_of(day, "momentum") == "sealed"

    def test_2008_to_2014_is_exploration_for_momentum(self):
        """已為動能族燒掉，因此納入探索區不新增成本。"""
        assert S.family_split_of(date(2008, 6, 1), "momentum") == "exploration"
        assert S.family_split_of(date(2014, 12, 31), "momentum") == "exploration"

    def test_the_seal_starts_at_2022_so_it_contains_a_down_year(self):
        """分界拉到 2022 的唯一理由：讓封存區含空頭 régime。"""
        assert S.family_split_of(date(2021, 12, 31), "momentum") == "exploration"
        assert S.family_split_of(date(2022, 1, 1), "momentum") == "sealed"

    def test_an_unregistered_family_raises_instead_of_guessing(self):
        with pytest.raises(ValueError, match="未登記的假說族"):
            S.family_split_of(date(2020, 1, 1), "reversal")


class TestForwardIsNotBacktestable:
    def test_forward_only_starts_the_day_the_journal_starts(self):
        assert S.family_split_of(date(2026, 8, 13), "momentum") == "sealed"
        assert S.family_split_of(date(2026, 8, 14), "momentum") == "forward_only"

    def test_slicing_forward_only_is_refused(self):
        """被回測吃掉就不再是前瞻證據，所以連取都不給取。"""
        with pytest.raises(ValueError, match="forward_only 不得用於回測"):
            S.family_slice_dates([date(2026, 9, 1)], "momentum", "forward_only")


class TestAccessIsAudited:
    def test_touching_the_seal_is_recorded_and_counted(self, monkeypatch, tmp_path):
        log = tmp_path / "log.md"
        monkeypatch.setattr(S, "_LOG", log)
        before = S.family_access_count("momentum", "sealed")

        S.family_slice_dates([date(2022, 6, 1)], "momentum", "sealed",
                             purpose="unit test")

        assert S.family_access_count("momentum", "sealed") == before + 1
        assert "momentum:sealed" in log.read_text(encoding="utf-8")

    def test_exploration_is_not_recorded_because_it_is_free(self, monkeypatch, tmp_path):
        log = tmp_path / "log.md"
        monkeypatch.setattr(S, "_LOG", log)
        S.family_slice_dates([date(2010, 6, 1)], "momentum", "exploration")
        assert not log.exists()

    def test_slicing_returns_only_the_dates_inside_the_split(self):
        days = [date(2021, 12, 31), date(2022, 1, 1), date(2026, 8, 14)]
        got = S.family_slice_dates(days, "momentum", "exploration")
        assert got == [date(2021, 12, 31)]


class TestCaveatsAreAvailableForCitation:
    def test_the_seal_declares_which_regime_it_lacks(self):
        """封存區沒有崩盤年——結論不得省略這句。"""
        assert "崩盤" in S.MOMENTUM_SEALED_CAVEAT
        assert "未經檢驗" in S.MOMENTUM_SEALED_CAVEAT

    def test_the_overlap_with_swing_tuning_is_disclosed(self):
        assert "w_trend_stack" in S.MOMENTUM_OVERLAP_DISCLOSURE


class TestOldSwingSplitIsUntouched:
    """波段族的舊切分標記為不可再動，這次改動不得碰它。"""

    def test_the_original_boundaries_still_hold(self):
        assert S.DEVELOPMENT == (date(2015, 1, 1), date(2020, 12, 31))
        assert S.VALIDATION == (date(2021, 1, 1), date(2022, 12, 31))
        assert S.HOLDOUT == (date(2023, 1, 1), date(2025, 5, 31))

    def test_the_global_entry_point_still_works(self, monkeypatch, tmp_path):
        monkeypatch.setattr(S, "_LOG", tmp_path / "log.md")
        assert S.slice_dates([date(2019, 6, 1)], "development") == [date(2019, 6, 1)]
