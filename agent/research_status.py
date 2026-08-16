"""研究資料地基的進度摘要，供 Streamlit「🔬 研究進度」頁使用。

資料來源只有 `reports/data_releases/*.json`（每份 8~9 KB，已進版控）。
這些是具名不可變釋出的**中繼資料**：元件清單、結構閘門結果、已知缺口、使用政策。

為什麼不讀 snapshot 本身
------------------------
`data/research_versions/` 與 `data/raw/` 都不進版控，Streamlit Cloud 上根本不存在；
而且釋出的 `usage_policy.blocked` 明文禁止「uploading raw research data to
Streamlit Cloud」。本模組只讀中繼資料，不碰任何研究資料。

為什麼不顯示績效
----------------
`usage_policy.blocked` 同時禁止 backward holdout 績效查看。本模組刻意不提供任何
報酬、Sharpe、回撤欄位——只有元件狀態、閘門旗標與缺口清單。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = ROOT / "reports" / "data_releases"

#: component_status 的中文說明與是否可用於策略推進。
#: 未知狀態一律視為「未就緒」，不做樂觀預設。
STATUS_LABELS: dict[str, tuple[str, bool]] = {
    "promotion_ready": ("可推進策略", True),
    "d4_component_ready": ("元件就緒", True),
    "price_only_structural_ready": ("僅價量，結構通過", False),
    "pit_universe_structural_ready": ("PIT universe，結構通過", False),
    "official_terms_structural_ready": ("官方條款，結構通過", False),
    "official_terms_settlement_incomplete": ("官方條款，交割資訊不全", False),
    "staging_blocked_for_strategy_promotion": ("staging，禁止推進策略", False),
    "d5_staging_complete_with_known_unknowns": ("staging 完成，有已知未知", False),
}

READINESS_LABELS: dict[str, str] = {
    "cross_machine_byte_verification_ready": "跨機位元組驗證",
    "twse_disposition_filter_component_ready": "TWSE 處置排除元件",
    "twse_altered_trading_filter_component_ready": "TWSE 變更交易排除元件",
    "pit_engine_correctness_work_ready": "PIT 引擎正確性工作",
    "backward_holdout_performance_ready": "backward holdout 績效開封",
    "all_market_strategy_promotion_ready": "全市場策略推進",
    "streamlit_runtime_requires_this_bundle": "Streamlit 執行需要此包",
}


@dataclass(frozen=True)
class ReleaseStatus:
    release_id: str
    release_date: str
    authority: str
    supersedes: str | None
    file_count: int
    total_bytes: int
    collection_sha256: str
    components: list[dict]
    readiness: dict[str, bool]
    known_gaps: list[str]
    usage_policy: dict[str, list[str]]
    source_path: Path

    @property
    def components_ready(self) -> int:
        return sum(1 for c in self.components if STATUS_LABELS.get(
            c.get("component_status"), ("", False))[1])

    @property
    def structural_failures(self) -> list[str]:
        return [c["component_id"] for c in self.components
                if not c.get("structural_passed", False)]


def available_releases() -> list[Path]:
    """回傳所有釋出定義檔，新的在前。"""
    if not RELEASE_DIR.is_dir():
        return []
    return sorted(RELEASE_DIR.glob("*.json"), reverse=True)


def load_release(path: str | Path | None = None) -> ReleaseStatus:
    """載入一份釋出定義；預設取最新一份。"""
    if path is None:
        candidates = available_releases()
        if not candidates:
            raise FileNotFoundError(f"找不到任何釋出定義：{RELEASE_DIR}")
        path = candidates[0]
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))

    missing = [k for k in ("data_release_id", "components", "transfer") if k not in payload]
    if missing:
        raise ValueError(f"{path.name} 缺少必要欄位：{missing}")

    transfer = payload["transfer"]
    return ReleaseStatus(
        release_id=payload["data_release_id"],
        release_date=payload.get("release_date", ""),
        authority=payload.get("authority", ""),
        supersedes=payload.get("supersedes"),
        file_count=int(transfer.get("file_count", 0)),
        total_bytes=int(transfer.get("total_bytes", 0)),
        collection_sha256=transfer.get("collection_sha256", ""),
        components=list(payload["components"]),
        readiness=dict(payload.get("readiness", {})),
        known_gaps=list(payload.get("known_gaps", [])),
        usage_policy=dict(payload.get("usage_policy", {})),
        source_path=path,
    )


def describe_status(component_status: str | None) -> tuple[str, bool]:
    """把 component_status 轉成（中文說明, 是否可推進策略）。未知狀態視為未就緒。"""
    if not component_status:
        return ("未標示", False)
    return STATUS_LABELS.get(component_status, (component_status, False))


def component_table(release: ReleaseStatus) -> list[dict]:
    """整理成前端可直接畫的列。刻意不含任何績效欄位。"""
    rows = []
    for c in release.components:
        label, ready = describe_status(c.get("component_status"))
        rows.append({
            "元件": c.get("component_id", ""),
            "狀態": label,
            "可推進策略": "✅" if ready else "—",
            "結構閘門": "✅" if c.get("structural_passed") else "❌",
            "content SHA-256": (c.get("content_sha256") or "")[:16],
            "snapshot 路徑": c.get("snapshot_path", ""),
        })
    return rows


def readiness_table(release: ReleaseStatus) -> list[dict]:
    rows = []
    for key, value in release.readiness.items():
        rows.append({
            "閘門": READINESS_LABELS.get(key, key),
            "狀態": "✅ 通過" if value else "🔒 未通過",
            "原始旗標": key,
        })
    return rows


def repeat_build_consistency(release: ReleaseStatus) -> dict:
    """比對 repeat_build_evidence 與 components 的 content SHA-256 是否逐項相同。

    這是「同一份 raw 重建兩次會不會得到同一份結果」的證據；不一致代表建構器不確定，
    任何下游研究結論都不可信。
    """
    payload = json.loads(release.source_path.read_text(encoding="utf-8-sig"))
    evidence = {e["component_id"]: e.get("content_sha256")
                for e in payload.get("repeat_build_evidence", [])}
    if not evidence:
        return {"checked": 0, "matched": 0, "mismatched": [], "available": False}

    matched, mismatched = 0, []
    for c in release.components:
        cid = c.get("component_id")
        if cid in evidence:
            if evidence[cid] == c.get("content_sha256"):
                matched += 1
            else:
                mismatched.append(cid)
    return {"checked": len(evidence), "matched": matched,
            "mismatched": mismatched, "available": True}
