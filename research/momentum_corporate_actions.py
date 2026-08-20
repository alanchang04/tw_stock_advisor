"""MOM-1 持有期間遇到公司行動時的實際股數處理（2026-08-13 凍結）。

問題
----
D3 的 6,275 個公司行動事件中有 653 筆 `ledger_status=blocked`，因為缺少官方
條件而無法決定「舊股東實際拿到幾股」。這曾是 F0 的 remaining blocker：
股數算錯，報酬就算錯。

但 2026-08-13 實測顯示問題比帳面小得多——**653 筆裡只有 10 筆落在 MOM-1
實際持有期間內**（1,070 個股-月中的 10 個，0.93%），且分成性質完全不同的兩類：

| 類型 | 筆數 | 是否可選擇 |
|---|---:|---|
| 現金增資（認購條件或交割時點缺失） | 9 | **可以不認購** |
| 無償配股（官方配股條件缺失） | 1 | **強制，股數必變** |

凍結的兩條政策
--------------
**政策 A：現金增資一律不認購。**
不認購時股東什麼都不必做：股數不變、價格吃除權跌幅、不支付認購款。
這是真實會發生的經濟結果，而且**完全不需要那些缺失的認購條件**。
方向保守——放棄認購權的價值會低估報酬而非高估（台股原股東認購權不可單獨
轉讓，因此放棄即純稀釋，語意乾淨）。

**政策 B：強制性公司行動若無法決定股數倍率，該部位以事件前最後收盤價平倉。**
無償配股是強制的，股數一定改變，無法比照政策 A 迴避。此時不猜倍率，
改為在事件前一個交易日以收盤價結清並轉為現金。明確、可稽核，
且實測只影響 1 個股-月（0.09%）。

為什麼不從價格反推
------------------
除權參考價的變動同時包含員工紅利與現增稀釋，那些**不是舊股東拿到的股票**。
以 `(前收 − 現金股利) / 除權參考價` 當配股倍率會把稀釋誤計為持股增加。
D3 第三輪已經因此把 50 筆以價格比反推的事件降回 blocked，本模組不重蹈覆轍。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

# 可選擇放棄的公司行動：不認購即無事發生
OPTIONAL_BLOCK_REASONS = frozenset({
    "paid_subscription_terms_missing",
    "paid_subscription_settlement_timing_missing",
})

Outcome = Literal["applied", "declined_subscription", "forced_close"]


@dataclass(frozen=True)
class ShareAdjustment:
    shares: int
    cash_delta: float
    outcome: Outcome
    reason: str

    @property
    def position_closed(self) -> bool:
        return self.outcome == "forced_close"


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def apply_corporate_action(
    *,
    shares: int,
    event: dict,
    pre_event_close: float | None = None,
) -> ShareAdjustment:
    """回傳公司行動後的實際股數與現金變動。

    ``event`` 需含 ``ledger_status``；blocked 時需 ``ledger_block_reason``。
    可執行事件使用 ``share_multiplier`` 與 ``cash_per_old_share``。

    政策 A（現金增資不認購）與政策 B（無法決定倍率即平倉）見模組 docstring。
    """
    if shares < 0:
        raise ValueError("shares 不可為負")
    status = event.get("ledger_status")

    if status == "executable":
        multiplier = _as_float(event.get("share_multiplier"))
        cash_per_share = _as_float(event.get("cash_per_old_share")) or 0.0
        if multiplier is None or multiplier <= 0:
            raise ValueError(
                "executable 事件必須有正的 share_multiplier；缺值代表 ledger 有誤，"
                "不得以 1.0 靜默通過"
            )
        # 只取整數股；畸零部分依 D3 契約另存 fractional entitlement，不灌回股數。
        return ShareAdjustment(
            shares=int(shares * multiplier),
            cash_delta=shares * cash_per_share,
            outcome="applied",
            reason="executable",
        )

    if status != "blocked":
        raise ValueError(f"未知的 ledger_status: {status!r}")

    reason = event.get("ledger_block_reason") or ""
    if reason in OPTIONAL_BLOCK_REASONS:
        # 政策 A：不認購。股數不變、不付款；價格已於除權日反映稀釋。
        return ShareAdjustment(
            shares=shares, cash_delta=0.0,
            outcome="declined_subscription", reason=reason,
        )

    # 政策 B：強制性公司行動且倍率未知 → 以事件前收盤價平倉。
    close = _as_float(pre_event_close)
    if close is None or close <= 0:
        raise ValueError(
            f"政策 B 需要事件前收盤價才能平倉（{reason}）；缺價不得假成交"
        )
    return ShareAdjustment(
        shares=0, cash_delta=shares * close,
        outcome="forced_close", reason=reason,
    )


def classify_ledger(events: pd.DataFrame) -> pd.DataFrame:
    """為 ledger 逐列標註本政策下的處理方式，供稽核與影響量測。"""
    required = {"ledger_status", "ledger_block_reason"}
    missing = required - set(events.columns)
    if missing:
        raise ValueError(f"ledger 缺少必要欄位: {sorted(missing)}")
    frame = events.copy()
    blocked = frame["ledger_status"].eq("blocked")
    optional = frame["ledger_block_reason"].isin(OPTIONAL_BLOCK_REASONS)
    frame["policy_outcome"] = "applied"
    frame.loc[blocked & optional, "policy_outcome"] = "declined_subscription"
    frame.loc[blocked & ~optional, "policy_outcome"] = "forced_close"
    return frame
