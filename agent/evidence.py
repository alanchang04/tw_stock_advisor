"""證據等級標記（Evidence Badge）。

為什麼需要這個
--------------
畫面上只有數字時，`t=2.73` 和 `t=2.73 [SELECTED DEVELOPMENT]` 看起來一樣，
但意思完全不同——前者像結論，後者是「在挑選過的開發資料上算出來的，不是確認」。
本模組把研究紀律變成介面的一部分：**每個數字都必須說出它的證據等級**。

詞彙來源
--------
**權威來源是 `research/results/evidence_registry.json` 的 `tier_vocabulary`**
（研究機產出，`scripts/build_evidence_registry.py`）。本模組開機時讀它，
讀不到才退回下面的 `FALLBACK_TIERS`——那是舊研發機依
`research/HYPOTHESES.md` 整理的版本，只當救生艇，不當第二套真相。

註冊表的 `display_rule` 明文規定：

> 任何取自某一列的數字，**必須**與該列的 `evidence_tier` 與 `mandatory_caveat`
> 併陳。面向使用者的介面的風險是誤讀，不是污染——所以解法是強制標註，
> 不是隱藏數字。唯一例外是 `sealed`：那裡沒有任何結果數字，顯示什麼都等於
> 暗示我們看過了。

因此本模組只負責「標註」，**不負責決定能不能顯示**——後者看每一列的
`display_policy`（full / summary_only / status_only / do_not_display）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

REGISTRY = (Path(__file__).resolve().parents[1]
            / "research" / "results" / "evidence_registry.json")

#: 徽章外觀：純顯示用，與語意定義分離。未列出的等級走 UNKNOWN。
_STYLE: dict[str, tuple[str, str, bool]] = {
    # key: (徽章文字, emoji, 能不能當確認證據)
    "development":   ("DEVELOPMENT", "🧪", False),
    "backward":      ("CLEAN BACKWARD", "🔒", True),
    "holdout_spent": ("HOLDOUT SPENT", "🚫", True),
    "sealed":        ("SEALED", "📦", False),
    "forward":       ("FORWARD", "⏩", True),
}


@dataclass(frozen=True)
class Tier:
    key: str
    label: str          # 徽章上的字
    emoji: str
    meaning: str        # 一句話解釋，給 tooltip / caption
    is_confirmation: bool   # 這個等級能不能當「確認」證據


#: 救生艇：註冊表讀不到時才用。不是第二套真相，僅供離線／測試。
FALLBACK_TIERS: dict[str, str] = {
    "development": "2015~2026。falsification / development evidence only，不是 OOS 確認",
    "backward": "2008~2014，該訊號在此段先前無 outcome exposure 的一次性集合",
    "holdout_spent": "一次性 holdout，已開封且不得重跑",
    "sealed": "資料已備妥但尚未與未來報酬 join",
    "forward": "2026-08-14 之後累積中，唯一的真確認來源",
}

#: 未知等級一律走這個——不做樂觀預設。
UNKNOWN = Tier("unknown", "UNVERIFIED", "❓",
               "沒有標記證據等級。在被標記之前，不得當成任何等級的證據。",
               is_confirmation=False)


def load_registry() -> dict:
    """讀取研究機策展的完整 registry；失敗時回空 dict，不猜內容。"""
    try:
        payload = json.loads(REGISTRY.read_text(encoding="utf-8-sig"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


_REGISTRY_PAYLOAD = load_registry()


def _load_vocabulary() -> dict[str, str]:
    vocab = _REGISTRY_PAYLOAD.get("tier_vocabulary") or {}
    if vocab:
        return {str(k): str(v) for k, v in vocab.items()}
    return dict(FALLBACK_TIERS)


def _build_tiers() -> dict[str, Tier]:
    tiers: dict[str, Tier] = {}
    for key, meaning in _load_vocabulary().items():
        label, emoji, confirm = _STYLE.get(key, (key.upper(), "🏷️", False))
        tiers[key] = Tier(key, label, emoji, meaning, confirm)
    return tiers


#: 由註冊表驅動。研究機新增等級時這裡自動跟上；樣式沒定義就用 key 當標籤，
#: 且 is_confirmation 預設 False——新等級不會因為沒人更新前端就被當成確認證據。
TIERS: dict[str, Tier] = _build_tiers()

TIER_SOURCE = ("evidence_registry"
               if _REGISTRY_PAYLOAD.get("tier_vocabulary") else "fallback")


def invalidated_artifacts() -> list[dict]:
    """回傳 registry 的作廢清單；缺欄位時安全地回空清單。"""
    rows = _REGISTRY_PAYLOAD.get("invalidated_artifacts") or []
    return [dict(row) for row in rows if isinstance(row, dict)]


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
