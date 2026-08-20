"""M23：全專案唯一資訊可得時鐘。

**這個測試檔存在的理由是一次具體的失敗。** 月營收的可得時點在本專案裡
長出了兩套並存的慣例（次月 10 日 vs 次月月末），兩邊各自都沒有前視、
各自都自洽，因此整整一輪研究沒有任何測試變紅。修正它的時候又發現，
兩邊**同時**都把「10 日」寫成 ``side="left"``，也就是把 10 日當天納入——
公司若在 10 日盤後才申報，用當天收盤價進場就是偷看一天。

所以這裡測三件事：
1. 可得時點的定義本身正確（嚴格晚於期限日）
2. 前視守門真的會擋
3. **這個定義在全專案只有一份**——這是 M23 的重點，也是最容易腐化的性質
"""
from __future__ import annotations

from pathlib import Path
import re

import pandas as pd
import pytest

from research.information_clock import (
    MONTHLY_REVENUE_DEADLINE_DAY,
    RULES,
    assert_decision_is_legal,
    earliest_executable_session,
    first_session_strictly_after,
    known_at,
    rule,
)

ROOT = Path(__file__).resolve().parents[1]


def business_sessions(start: str, end: str) -> pd.DatetimeIndex:
    """用工作日近似交易日曆；本檔測的是日期算術，不需要真實休市表。"""
    return pd.bdate_range(start, end)


class TestCanonicalAvailability:
    def test_monthly_revenue_is_due_the_month_after_the_revenue_month(self):
        # 2020 年 1 月的營收，法定期限是 2020 年 2 月 10 日
        moment = known_at("monthly_revenue", "2020-01")
        assert moment.year == 2020 and moment.month == 2
        assert moment.day == MONTHLY_REVENUE_DEADLINE_DAY

    def test_known_at_is_the_end_of_the_deadline_day_not_its_start(self):
        """法規只規範日期不規範時刻，因此保守可得時點是「當天結束」。

        用日期表示無法區分「10 日」與「10 日結束」，而那正是本模組要
        消滅的歧義——所以回傳值必須帶時分秒。
        """
        moment = known_at("monthly_revenue", "2020-01")
        assert (moment.hour, moment.minute) == (23, 59)

    def test_unregistered_data_types_raise_instead_of_guessing(self):
        """呼叫端自己推一個可得時點，正是 M23 要防的事。"""
        with pytest.raises(KeyError, match="沒有登記"):
            known_at("quarterly_earnings", "2020-01")

    def test_period_must_be_monthly(self):
        with pytest.raises(ValueError, match="月頻"):
            rule("monthly_revenue").known_at(pd.Period("2020", freq="Y"))


class TestStrictlyAfterDeadline:
    """**這一組是本次修正的核心。** 期限日當天不得使用。"""

    def test_the_deadline_day_itself_is_never_selected(self):
        # 2020-02-10 是星期一，是交易日。它**不能**被選中——
        # 公司可能在當天盤後才申報，用當天收盤價進場等於偷看一天。
        sessions = business_sessions("2020-01-01", "2020-03-31")
        assert pd.Timestamp("2020-02-10") in sessions

        chosen = earliest_executable_session("monthly_revenue", "2020-01", sessions)
        assert chosen == pd.Timestamp("2020-02-11")

    def test_left_side_search_would_have_picked_the_deadline_day(self):
        """對照組：證明上一個測試不是恆真句。

        這是專案裡原本的寫法（``side="left"``），它會選中 10 日當天。
        兩者若給出相同答案，上面那個測試就沒在測東西。
        """
        sessions = business_sessions("2020-01-01", "2020-03-31")
        deadline_start = pd.Timestamp("2020-02-10")
        wrong = sessions[sessions.searchsorted(deadline_start, side="left")]
        assert wrong == pd.Timestamp("2020-02-10")
        assert wrong != earliest_executable_session(
            "monthly_revenue", "2020-01", sessions)

    def test_when_the_deadline_falls_on_a_holiday_the_next_session_is_used(self):
        # 2020-05-10 是星期日；下一個交易日是 5/11 星期一
        sessions = business_sessions("2020-04-01", "2020-06-30")
        assert pd.Timestamp("2020-05-10") not in sessions
        chosen = earliest_executable_session("monthly_revenue", "2020-04", sessions)
        assert chosen == pd.Timestamp("2020-05-11")

    def test_returns_none_when_the_calendar_does_not_reach_the_deadline(self):
        """日曆走不到就回 None，不要靜默給出最後一個交易日。"""
        sessions = business_sessions("2020-01-01", "2020-01-31")
        assert earliest_executable_session(
            "monthly_revenue", "2020-01", sessions) is None

    def test_first_session_strictly_after_excludes_an_exact_match(self):
        sessions = pd.DatetimeIndex(["2020-01-02", "2020-01-03", "2020-01-06"])
        assert (first_session_strictly_after(sessions, pd.Timestamp("2020-01-03"))
                == pd.Timestamp("2020-01-06"))


class TestLookAheadGuard:
    def test_a_decision_before_the_earliest_executable_session_is_rejected(self):
        sessions = business_sessions("2020-01-01", "2020-03-31")
        with pytest.raises(ValueError, match="前視"):
            assert_decision_is_legal("monthly_revenue", "2020-01",
                                     pd.Timestamp("2020-02-10"), sessions)

    def test_a_later_decision_is_allowed_because_late_is_conservative(self):
        """月末慣例比法定期限晚，方向保守，因此**合法**。

        M23 擋的是前視，不是強迫所有研究都改用最早時點——
        用多晚是研究設計選擇，不是正確性問題。
        """
        sessions = business_sessions("2020-01-01", "2020-03-31")
        assert_decision_is_legal("monthly_revenue", "2020-01",
                                 pd.Timestamp("2020-02-28"), sessions)


class TestSingleDefinition:
    """M23 的重點性質：這個定義在全專案只能有一份。

    前兩組測的是「定義正確」；這一組測的是「沒有第二份定義」。
    上次出事的正是後者——兩份定義各自都正確，但彼此不一致。
    """

    # **不要寫死搜尋目錄。** 第一版把它寫成 ("research", "scripts", "agent")，
    # 於是漏掉了 `margin_reversal/`——那裡有第三份實作，帶著同一個偏一天的
    # 錯誤。一個防止「定義擴散」的測試，本身卻只看固定幾個地方，等於留了
    # 一個形狀完全相同的洞。改成自動列舉所有套件目錄。
    EXCLUDED_ROOTS = {"tests", "handoff", "docs", "reports", "data", "notebooks"}
    # 「次月 10 日」這個規則的各種手寫實作形態
    REIMPLEMENTATION = re.compile(
        r"DEADLINE_DAY\s*=\s*\d"           # 自己定一個期限常數
        r"|Timedelta\(days\s*=\s*9\)"      # 月初 + 9 天 = 10 日（本專案原本的寫法）
    )
    ALLOWED = {Path("research/information_clock.py")}

    @classmethod
    def package_roots(cls) -> list[Path]:
        return sorted(
            path for path in ROOT.iterdir()
            if path.is_dir()
            and not path.name.startswith((".", "_"))
            and path.name not in cls.EXCLUDED_ROOTS
            and any(path.rglob("*.py")))

    def test_the_scan_actually_covers_the_known_packages(self):
        """先確認搜尋範圍沒有塌掉——否則下面那個測試會靜默地永遠通過。"""
        names = {path.name for path in self.package_roots()}
        assert {"research", "scripts", "agent", "margin_reversal"} <= names

    def test_no_other_module_reimplements_the_revenue_deadline(self):
        offenders = []
        for root in self.package_roots():
            for path in root.rglob("*.py"):
                relative = path.relative_to(ROOT)
                if Path(*relative.parts) in self.ALLOWED:
                    continue
                if self.REIMPLEMENTATION.search(path.read_text(encoding="utf-8")):
                    offenders.append(str(relative))
        assert not offenders, (
            "以下檔案自己實作了月營收期限規則，請改呼叫 "
            "research.information_clock：\n  " + "\n  ".join(sorted(offenders)))

    def test_the_registry_has_at_most_one_rule_per_data_type(self):
        assert len(RULES) == len({r.data_type for r in RULES.values()})

    def test_every_rule_records_its_legal_basis(self):
        """沒有法源的規則就是某個人的猜測，下次沒人敢動它。"""
        for name, item in RULES.items():
            assert item.legal_basis.strip(), f"{name} 沒有寫法源"
