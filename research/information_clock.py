"""全專案唯一的「資訊何時可得」時鐘（`docs/RESEARCH_METHOD_AMENDMENTS.md` M23）。

為什麼需要這個模組
------------------
2026-08-14 發現：同一個資訊事件（月營收公布）在本專案裡有**兩套並存的慣例**。

    scripts/run_h04_h06_event_study.py   ->  次月 10 日後第一個交易日
    H11 / H12 / H16 鏈                   ->  次月月末（晚約 14 個交易日）

兩邊**各自都沒有前視、各自都自洽**，所以整整一輪研究都沒有任何測試變紅。
M1~M22 沒有一條擋得住——它們管的是估計與推論，不管「語意一致性」。

代價是可量測的：被跳過的那段窗，用 H11 自己的估計式（Rev+ dummy 橫斷面
斜率、控制規模）測得 **+1.045%／月（t=9.44）**，比 H11 自己的 M+1
（+0.936%）還大，而且只用了約七成的時間。**峰值不在原本的量測範圍內。**

因此本模組成為單一事實來源：event study、Fama-MacBeth、組合回測、
forward journal、特徵生成，全部呼叫這裡，不得各自實作。

保守原則：「結束後」而不是「當天」
----------------------------------
法規只說「**次月 10 日以前**申報」，沒有說當天幾點。公司可能在 10 日
盤中、盤後、或更早申報。因此：

- 10 日**收盤價**不能安全使用——盤後才申報的公司，那個價格還沒反映資訊，
  拿它進場等於偷看了一天。
- 但到 10 日**整天結束**，法定窗口已經完成，全體必然已申報。

所以可安全執行的最早時點是「**10 日結束後的第一個交易日開盤**」，
也就是嚴格晚於 10 日的第一個交易日。程式上這是 ``side="right"``；
寫成 ``side="left"`` 會把 10 日本身納入，那正是上面說的偷看一天。

這一天的差別不是理論潔癖：本專案第一版診斷就是寫成 ``side="left"``。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# 台灣證券交易所規定：上市公司應於次月 10 日以前申報上月營業額。
# **這個常數只能在這裡出現一次。** 見 tests/test_information_clock.py
# 裡的 TestSingleDefinition，它會讓任何在別處重新定義的行為變紅。
MONTHLY_REVENUE_DEADLINE_DAY = 10


@dataclass(frozen=True)
class AvailabilityRule:
    """一種資料型態的「保守可得時點」規則。

    ``known_at`` 回傳的是**時刻**（含時分秒），不是日期。這是刻意的：
    「10 日」與「10 日結束」差一個交易日，用日期表示無法區分這兩者，
    而那正是本模組要消滅的歧義。
    """

    data_type: str
    legal_basis: str
    lag_months: int
    deadline_day: int

    def known_at(self, period: pd.Period) -> pd.Timestamp:
        """``period`` 是**資料所屬期間**（例如營收月），回傳保守可得時刻。

        回傳「期限日的結束」（23:59:59），因為法規只規範日期不規範時刻。
        """
        if period.freqstr != "M":
            raise ValueError(f"{self.data_type} 的期間必須是月頻，收到 {period.freqstr}")
        due_month = (period + self.lag_months).to_timestamp(how="start")
        return pd.Timestamp(year=due_month.year, month=due_month.month,
                            day=self.deadline_day,
                            hour=23, minute=59, second=59)

    def earliest_executable(self, period: pd.Period,
                            sessions: pd.DatetimeIndex) -> pd.Timestamp | None:
        """可安全執行的最早交易時點：``known_at`` 之後第一個交易日的開盤。

        **嚴格晚於**期限日。回傳 ``None`` 代表交易日曆還沒走到那裡。
        """
        return first_session_strictly_after(sessions, self.known_at(period))


# ── 註冊表：一個資料型態只准有一條規則 ──────────────────────────────
RULES: dict[str, AvailabilityRule] = {
    "monthly_revenue": AvailabilityRule(
        data_type="monthly_revenue",
        legal_basis="TWSE：上市公司應於次月 10 日以前申報上月營業額",
        lag_months=1,
        deadline_day=MONTHLY_REVENUE_DEADLINE_DAY,
    ),
}


def rule(data_type: str) -> AvailabilityRule:
    if data_type not in RULES:
        raise KeyError(
            f"{data_type!r} 沒有登記可得時點規則。不要在呼叫端自己推一個——"
            f"在 research/information_clock.py 的 RULES 補上它。"
            f"目前已登記：{sorted(RULES)}")
    return RULES[data_type]


def known_at(data_type: str, period: pd.Period | str) -> pd.Timestamp:
    """資料型態 ``data_type``、期間 ``period`` 的保守可得**時刻**。"""
    return rule(data_type).known_at(pd.Period(period, freq="M"))


def first_session_strictly_after(sessions: pd.DatetimeIndex,
                                 moment: pd.Timestamp) -> pd.Timestamp | None:
    """``moment`` 之後第一個交易日。等於 ``moment`` 的交易日**不算**。

    ``sessions`` 通常是午夜對齊的日期索引，因此當 ``moment`` 是
    「10 日 23:59:59」時，10 日這個 session（10 日 00:00）自然落在它之前，
    正確地被排除。
    """
    sessions = pd.DatetimeIndex(sessions)
    position = sessions.searchsorted(moment, side="right")
    if position >= len(sessions):
        return None
    return sessions[position]


def earliest_executable_session(data_type: str, period: pd.Period | str,
                                sessions: pd.DatetimeIndex) -> pd.Timestamp | None:
    """可安全買進的最早交易日（在該日**開盤**成交）。"""
    return rule(data_type).earliest_executable(pd.Period(period, freq="M"), sessions)


def assert_decision_is_legal(data_type: str, period: pd.Period | str,
                             decision: pd.Timestamp,
                             sessions: pd.DatetimeIndex) -> None:
    """守門：決策日不得早於該資料的最早可執行日，否則就是前視。

    比 ``known_at`` 晚是**允許**的（月末慣例就是如此，方向保守）；
    早才是錯的。這個函式只擋前視，不強迫大家都改用最早時點——
    那是研究設計選擇，不是正確性問題。
    """
    earliest = earliest_executable_session(data_type, period, sessions)
    if earliest is None:
        raise ValueError(f"{data_type} {period}：交易日曆尚未涵蓋其可得時點")
    decision = pd.Timestamp(decision)
    if decision < earliest:
        raise ValueError(
            f"前視：{data_type} {period} 最早可執行日是 {earliest.date()}，"
            f"但決策日設在 {decision.date()}。"
            f"法源：{rule(data_type).legal_basis}")
