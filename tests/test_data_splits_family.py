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
        """2026-01-15：對波段是硬污染，對動能是 development。

        兩者都不可用於驗收，但**理由不同**——波段是調參目標窗，
        動能是形成窗的 IC 選擇集合。族別切分的重點是理由要分開記錄。
        """
        day = date(2026, 1, 15)
        assert S.family_split_of(day, "swing") == "contaminated"
        assert S.family_split_of(day, "momentum") == "development"

    def test_momentum_has_no_sealed_split_at_all(self):
        """**這是本檔最重要的一條。**

        2026-08-16 曾鎖死 2008-2021 探索 / 2022-2026.8 封存，隨即撤回：
        SPEC §9.2.4 明載 mom_6_1 的形成窗是看著 2015~2026 的 IC 表從四個
        候選中挑出來的，而 2008~2014 又被 F1 開封兩次。乾淨歷史 holdout = 0。

        若有人日後想重新引入 sealed，這條會擋下來，並強迫他先解釋
        §9.2.4 為什麼不適用。
        """
        assert "sealed" not in S.FAMILY_SPLITS["momentum"]

    def test_the_whole_history_is_development_for_momentum(self):
        for day in (date(2008, 6, 1), date(2014, 12, 31),
                    date(2021, 12, 31), date(2022, 1, 1), date(2026, 8, 13)):
            assert S.family_split_of(day, "momentum") == "development", day

    def test_an_unregistered_family_raises_instead_of_guessing(self):
        with pytest.raises(ValueError, match="未登記的假說族"):
            S.family_split_of(date(2020, 1, 1), "reversal")


class TestForwardIsNotBacktestable:
    def test_forward_only_starts_the_day_the_journal_starts(self):
        assert S.family_split_of(date(2026, 8, 13), "momentum") == "development"
        assert S.family_split_of(date(2026, 8, 14), "momentum") == "forward_only"

    def test_slicing_forward_only_is_refused(self):
        """被回測吃掉就不再是前瞻證據，所以連取都不給取。"""
        with pytest.raises(ValueError, match="forward_only 不得用於回測"):
            S.family_slice_dates([date(2026, 9, 1)], "momentum", "forward_only")


class TestAccessIsAudited:
    def test_swing_holdout_is_still_recorded(self, monkeypatch, tmp_path):
        log = tmp_path / "log.md"
        monkeypatch.setattr(S, "_LOG", log)
        before = S.family_access_count("swing", "holdout")

        S.family_slice_dates([date(2023, 6, 1)], "swing", "holdout",
                             purpose="unit test")

        assert S.family_access_count("swing", "holdout") == before + 1
        assert "swing:holdout" in log.read_text(encoding="utf-8")

    def test_momentum_development_is_not_recorded_because_it_is_already_spent(
            self, monkeypatch, tmp_path):
        """已經沒有東西可以再耗盡了，所以不記帳。"""
        log = tmp_path / "log.md"
        monkeypatch.setattr(S, "_LOG", log)
        S.family_slice_dates([date(2010, 6, 1)], "momentum", "development")
        assert not log.exists()

    def test_slicing_returns_only_the_dates_inside_the_split(self):
        days = [date(2007, 12, 31), date(2010, 6, 1), date(2026, 8, 14)]
        got = S.family_slice_dates(days, "momentum", "development")
        assert got == [date(2010, 6, 1)]


class TestCaveatsAreAvailableForCitation:
    def test_the_caveat_states_there_is_no_clean_holdout(self):
        """動能族的歷史結論一律要附這句，沒有例外。"""
        assert "乾淨歷史 holdout = 0" in S.MOMENTUM_DEVELOPMENT_CAVEAT
        assert "forward" in S.MOMENTUM_DEVELOPMENT_CAVEAT

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
