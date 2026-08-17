"""證據等級標記（Evidence Badge）。

為什麼需要這個
--------------
畫面上只有數字時，`t=2.73` 和 `t=2.73 [SELECTED DEVELOPMENT]` 看起來一樣，
但意思完全不同——前者像結論，後者是「在挑選過的開發資料上算出來的，不是確認」。
本模組把研究紀律變成介面的一部分：**每個數字都必須說出它的證據等級**。

詞彙來源
--------
沿用 `research/HYPOTHESES.md` 與 `docs/RESEARCH_V2_PIPELINE.md` 既有的說法
（M22 兩軌：Discovery/development vs OOS 確認），**不新造**：

- development：2015~2026，falsification / development evidence only，不是 OOS
- backward：2008~2014，未看過的一次性集合
- sealed：已密封，尚未開封
- forward：2026-08-14 之後累積中

⚠️ `TIERS` 目前是本機依上述文件整理的，尚待研究機以
`research/results/evidence_registry.json` 的 `tier_vocabulary` 確認
（見 reports/REQUEST_2026-08-18_EVIDENCE_DASHBOARD_AND_COCKPIT.md §2）。
確認後應改為讀那份檔，不要兩邊各維護一份。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tier:
    key: str
    label: str          # 徽章上的字
    emoji: str
    meaning: str        # 一句話解釋，給 tooltip / caption
    is_confirmation: bool   # 這個等級能不能當「確認」證據


TIERS: dict[str, Tier] = {
    "development": Tier(
        "development", "DEVELOPMENT", "🧪",
        "2015~2026 開發資料。可用於否證與機制探索，**不是** OOS 確認；"
        "同一段資料已被多次查看，數字帶選擇偏誤。",
        is_confirmation=False),
    "backward": Tier(
        "backward", "CLEAN BACKWARD", "🔒",
        "2008~2014 未看過的一次性集合。只能開封一次，"
        "開封後不得再用於探索或調參。",
        is_confirmation=True),
    "sealed": Tier(
        "sealed", "SEALED", "📦",
        "已密封、尚未開封。目前沒有任何績效數字可看。",
        is_confirmation=False),
    "forward": Tier(
        "forward", "FORWARD", "⏩",
        "2026-08-14 之後累積的前向紀錄。樣本仍在成長，"
        "未達預定期數前不得下結論。",
        is_confirmation=True),
    "inconclusive": Tier(
        "inconclusive", "INCONCLUSIVE", "⚪",
        "主要判準未通過或功效不足。既不支持也不否決假說，不得當成任一方的證據。",
        is_confirmation=False),
}

#: 未知等級一律走這個——不做樂觀預設。
UNKNOWN = Tier("unknown", "UNVERIFIED", "❓",
               "沒有標記證據等級。在被標記之前，不得當成任何等級的證據。",
               is_confirmation=False)


def tier(key: str | None) -> Tier:
    """取等級定義。未知或缺值一律回 UNKNOWN，不猜。"""
    if not key:
        return UNKNOWN
    return TIERS.get(str(key).strip().lower().replace(" ", "_"), UNKNOWN)


def badge(key: str | None) -> str:
    """回傳可直接放進 markdown 的徽章字串。"""
    t = tier(key)
    return f"{t.emoji} `{t.label}`"


def annotate(value: str, key: str | None) -> str:
    """數字 + 徽章。這是本模組最主要的用法。

    >>> annotate("0.92", "development")
    '0.92　🧪 `DEVELOPMENT`'
    """
    return f"{value}　{badge(key)}"


def caption(key: str | None) -> str:
    """徽章後面接的一句話解釋。"""
    t = tier(key)
    return f"{t.emoji} **{t.label}** — {t.meaning}"


def is_confirmation(key: str | None) -> bool:
    """這個等級能不能當確認證據。未知一律 False。"""
    return tier(key).is_confirmation
