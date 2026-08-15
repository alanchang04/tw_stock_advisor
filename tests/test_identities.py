"""M24 經濟恆等式檢查。

測試用的是**真實發生過的那組數字**：H11b 第一版與修正版。
這樣這個檔同時是回歸測試與案例紀錄——如果約束哪天被改鬆到抓不到
那個 bug，這裡就會紅。
"""
from __future__ import annotations

import pytest

from research.identities import (  # noqa: F401
    DEFAULT_TOLERANCE, IdentityCheck, additivity, containment,
)

# 診斷獨立測得：法定期限後到月末那段（FM 控制規模口徑）
EARLY_WINDOW = 0.842
# H11b 第一版的 M+1（多做一次前瞻位移 → 整條路徑後推一期）
BUGGED_M1 = 0.680
# 修正後的 M+1
CORRECT_M1 = 1.679


class TestContainment:
    def test_it_catches_the_bug_that_actually_happened(self):
        """M+1 的窗完整包含早窗，所以 M+1 不可能比早窗還小。

        **這條測試存在的第二個理由是校準。** 第一版把容忍度設成 0.30
        （誤用了個別係數的 SE 當噪音尺度），而實際違反量只有 0.162，
        於是約束靜靜地通過了——一個抓不到真實錯誤的約束等於沒有。
        """
        check = containment(label="H11b M+1 ⊇ 早窗",
                            whole=BUGGED_M1, part=EARLY_WINDOW)
        assert not check.holds, (
            f"容忍度過寬：違反量 {EARLY_WINDOW - BUGGED_M1:.3f} 沒有被抓到")
        with pytest.raises(ValueError, match="先查實作"):
            check.raise_if_violated()

    def test_a_remainder_floor_gives_the_constraint_a_comfortable_margin(self):
        """推薦用法：對剩餘段給下界，約束就不再是勉強抓到。

        M+1 的窗 = 早窗 ＋ 之後約 7 個交易日。那段已知是正漂移
        （對照窗實測 +0.21%／同長度），因此下界取 0.15 很保守。
        """
        check = containment(label="H11b M+1 ⊇ 早窗（含下界）",
                            whole=BUGGED_M1, part=EARLY_WINDOW,
                            remainder_floor=0.15)
        assert not check.holds
        assert check.slack < -0.15, "有下界時應明顯違反，而不是剛好違反"

    def test_the_corrected_value_passes(self):
        check = containment(label="H11b M+1 ⊇ 早窗",
                            whole=CORRECT_M1, part=EARLY_WINDOW)
        assert check.holds
        check.raise_if_violated()          # 不應拋出

    def test_tolerance_is_wide_enough_not_to_fire_on_sampling_noise(self):
        """約束要抓的是結構性錯誤，不是做顯著性檢定。

        正確的噪音尺度是**剩餘段**的 SE（約 0.08pp），不是個別係數的 SE
        ——包含窗與被包含窗共用重疊那段資料，差值的變異只來自剩餘段。
        小於一個那樣的 SE 時不該觸發，否則它會變成噪音警報器，
        久了就沒有人看。
        """
        check = containment(label="噪音",
                            whole=EARLY_WINDOW - 0.08, part=EARLY_WINDOW)
        assert check.holds

    def test_a_remainder_floor_makes_the_constraint_stricter(self):
        """若事前知道剩餘段至少貢獻多少，約束就可以收緊。"""
        loose = containment(label="無下界", whole=1.0, part=0.9)
        tight = containment(label="有下界", whole=1.0, part=0.9,
                            remainder_floor=0.5)
        assert loose.holds
        assert not tight.holds

    def test_slack_is_signed_so_reports_can_show_how_close_it_was(self):
        check = containment(label="x", whole=1.0, part=0.5)
        assert check.slack == pytest.approx(1.0 - (0.5 - DEFAULT_TOLERANCE))


class TestAdditivity:
    # H11b yoy>0 的實際反應路徑與 6M 累積
    H11B_PATH = [1.679, 0.661, 0.529, 0.258, 0.224, 0.068]
    H11B_CUMULATIVE = 3.251

    def test_path_segments_sum_to_the_cumulative(self):
        check = additivity(label="H11b 路徑加總", whole=self.H11B_CUMULATIVE,
                           parts=self.H11B_PATH)
        assert check.holds

    def test_the_systematic_gap_is_real_and_must_not_be_read_as_an_error(self):
        """逐期加總比累積大約 0.17pp——**這是設計使然，不是錯誤**。

        `cumulative_from_path` 要求同一格點六個領先期都存在，於是丟掉尾端
        幾個格點；逐期係數各自用自己能用的格點。H11 也有同量級的落差
        （2.555 vs 2.44）。容忍度若照 containment 設成 0.15，
        這個正常現象就會被誤報成錯誤。
        """
        gap = sum(self.H11B_PATH) - self.H11B_CUMULATIVE
        assert 0.10 < gap < 0.20
        assert not additivity(label="容忍度過緊", whole=self.H11B_CUMULATIVE,
                              parts=self.H11B_PATH, tolerance=0.15).holds

    def test_a_small_missing_segment_is_deliberately_not_caught(self):
        path = self.H11B_PATH[:-1]                        # 少了 M+6（僅 0.068）
        assert additivity(label="缺一小段", whole=self.H11B_CUMULATIVE,
                          parts=path).holds

    def test_a_whole_large_segment_going_missing_is_caught(self):
        assert not additivity(label="缺一大段", whole=self.H11B_CUMULATIVE,
                              parts=self.H11B_PATH[1:]).holds


class TestResultShape:
    def test_a_holding_check_never_raises(self):
        IdentityCheck(label="x", holds=True, detail="", slack=0.0
                      ).raise_if_violated()

    def test_the_message_names_the_constraint_and_the_numbers(self):
        check = containment(label="H11b M+1 ⊇ 早窗",
                            whole=BUGGED_M1, part=EARLY_WINDOW)
        assert "H11b M+1" in check.detail or "H11b M+1" in check.label
        assert "0.68" in check.detail and "0.84" in check.detail
