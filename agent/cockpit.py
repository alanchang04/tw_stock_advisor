"""首頁 Decision Cockpit 的唯讀、可測資料轉接層。"""
from __future__ import annotations

import json
from pathlib import Path

FORWARD_JOURNAL = (Path(__file__).resolve().parents[1]
                   / "reports" / "forward_journal.json")

REGIME_PRESENTATION = {
    "risk_on": ("多頭／Risk-on", "🟢"),
    "neutral": ("中性／Neutral", "🟡"),
    "risk_off": ("空頭／Risk-off", "🔴"),
}


def load_forward_status(path: Path = FORWARD_JOURNAL) -> dict:
    """讀 forward journal 摘要；缺檔/壞檔時顯式回報，不假裝為零期。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("forward journal root must be an object")
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    observations = payload.get("observations")
    if not isinstance(observations, list):
        return {"ok": False, "error": "observations 欄位不是 list"}

    freshness = payload.get("snapshot_freshness") or {}
    return {
        "ok": True,
        "forward_start": payload.get("forward_start"),
        "periods": len(observations),
        "last_trade_date": freshness.get("last_trade_date"),
        "lag_days": freshness.get("lag_days"),
        "is_stale": freshness.get("is_stale"),
        "threshold_days": freshness.get("threshold_days"),
    }


def current_strategy_status() -> dict:
    """以 STRATEGY_ERAS[0] 為唯一權威來源，不在 UI 另寫日期。"""
    from agent.strategy import STRATEGY_ERAS

    if not STRATEGY_ERAS:
        return {"ok": False, "error": "STRATEGY_ERAS 是空的"}
    era = STRATEGY_ERAS[0]
    return {
        "ok": True,
        "key": era.get("key"),
        "label": era.get("label"),
        "live_from": era.get("live_from"),
    }


def present_regime(detail: dict | None) -> dict:
    """將 market_regime_detail 轉為 UI 字串；ok=False 絕不畫成多頭。"""
    detail = detail or {}
    if not detail.get("ok"):
        return {
            "ok": False,
            "label": "狀態不可用",
            "icon": "⚪",
            "caption": "市場濾網查詢失敗；不得把保守 fallback 的 bull=True 畫成多頭。",
        }

    # main 的既有 helper 是 bull 二態；研究分支新版才有 state 三態。
    # release 不為了 UI 偷渡策略引擎變更：有 state 就原樣顯示，沒有就忠實呈現 bull 二態。
    has_three_state = detail.get("state") is not None
    state = str(detail.get("state") if has_three_state else
                ("risk_on" if detail.get("bull") else "risk_off"))
    label, icon = REGIME_PRESENTATION.get(state, (state, "⚪"))
    close = detail.get("close")
    ma60 = detail.get("ma60")
    breadth = detail.get("breadth")
    scale = detail.get("exposure_scale")
    parts = []
    if close is not None and ma60 is not None:
        parts.append(f"0050 {float(close):.2f}｜MA60 {float(ma60):.2f}")
    if breadth is not None:
        parts.append(f"市場寬度 {float(breadth) * 100:.0f}%")
    if scale is not None:
        parts.append(f"曝險倍率 {float(scale):.0%}")
    if not has_three_state:
        parts.append("目前部署版市場濾網為 bull 二態")
    return {
        "ok": True,
        "label": label,
        "icon": icon,
        "caption": "｜".join(parts),
    }
