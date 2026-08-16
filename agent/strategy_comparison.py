"""策略比較頁的資料層。

權威來源
--------
``reports/strategy_comparison_metrics.json``（研究機產出，
`scripts/build_strategy_comparison_metrics.py`）。**前端不重算任何指標**——
自己算一次 Sharpe 就會產生第二個真相，之後對不上時沒人知道該信哪個。

該檔不存在時退回 adapter，從各自原始報告取值並標記 ``provisional=True``，
頁面上會明示尚未正規化。

刻意不做的事
------------
- **不排名、不給綜合評分。** 期間、régime、樣本單位都不同，排名會製造
  一個不存在的可比性。權威檔自己的 ``purpose`` 就寫著
  "to GENERATE hypotheses, not to pick a winner"。
- **不顯示被抑制的指標。** margin reversal 的 Sharpe／MDD／年化在
  0.33 個日曆年上不具統計意義，權威檔已把它們設為 null 並移入
  ``suppressed_because_sample_too_small`` 供稽核；本模組原樣保留該區塊，
  但不把數值搬回主要欄位。
- **不為 MOM-1 產生或顯示權益曲線。** 一次性 holdout 沒有持久化 NAV 序列，
  要畫就得重跑 2008–2014；且從 holdout 的失效形態找模式本身就是污染
  （見 reports/REPLY_2026-08-16_STRATEGY_COMPARISON_ARTIFACTS.md §1）。
  只有已公布的 7 個年度報酬可以呈現。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
CANONICAL = REPORTS / "strategy_comparison_metrics.json"

#: 判決狀態 → 顯示文字。未知狀態原樣顯示，不做樂觀預設。
STATUS_LABELS: dict[str, str] = {
    "frozen_satellite": "凍結・衛星",
    "rejected_at_F1": "F1 否決",
    "sample_insufficient": "樣本不足",
    "not_executed": "未執行",
}


@dataclass
class StrategyRow:
    key: str
    display_name: str
    status: str
    verdict_note: str | None = None
    period: tuple[str, str] | None = None
    factors: list[str] = field(default_factory=list)
    sharpe: float | None = None
    mdd: float | None = None
    ann_ret: float | None = None
    total_return: float | None = None
    win_rate: float | None = None
    trades: int | None = None
    independent_episodes: int | None = None
    observations: int | None = None
    calendar_years: float | None = None
    benchmark_key: str | None = None
    benchmark_sharpe: float | None = None
    benchmark_mdd: float | None = None
    curve_dir: str | None = None
    annual_returns: dict[str, float] = field(default_factory=dict)
    regime_caveat: str | None = None
    suppressed: dict = field(default_factory=dict)
    source_report: str | None = None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def has_curve(self) -> bool:
        return bool(self.curve_dir) and (ROOT / self.curve_dir).is_dir()

    @property
    def sample_text(self) -> str:
        """樣本數的人話版。單位不同就不要假裝是同一個數。"""
        bits = []
        if self.trades is not None:
            bits.append(f"{self.trades} 筆交易")
        if self.independent_episodes is not None:
            bits.append(f"{self.independent_episodes} 個獨立情境")
        if self.observations is not None:
            bits.append(f"{self.observations} 個觀測")
        if self.calendar_years is not None:
            bits.append(f"{self.calendar_years:.2f} 年")
        return "、".join(bits) if bits else "—"

    @property
    def sample_is_adequate(self) -> bool | None:
        n = self.independent_episodes if self.independent_episodes is not None else self.trades
        return None if n is None else n >= 30


def _read(path: Path) -> dict | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _row_from_canonical(item: dict) -> StrategyRow:
    period = item.get("period")
    return StrategyRow(
        key=item["key"], display_name=item["display_name"], status=item["status"],
        verdict_note=item.get("verdict_note"),
        period=tuple(period) if period else None,
        factors=list(item.get("factors") or []),
        sharpe=item.get("sharpe"), mdd=item.get("mdd"),
        ann_ret=item.get("ann_ret"), total_return=item.get("total_return"),
        win_rate=item.get("win_rate"), trades=item.get("trades"),
        independent_episodes=item.get("independent_episodes"),
        observations=item.get("observations"),
        calendar_years=item.get("calendar_years"),
        benchmark_key=item.get("benchmark_key"),
        benchmark_sharpe=item.get("benchmark_sharpe"),
        benchmark_mdd=item.get("benchmark_mdd"),
        curve_dir=item.get("curve_dir"),
        annual_returns=dict(item.get("annual_returns") or {}),
        regime_caveat=item.get("regime_caveat"),
        suppressed=dict(item.get("suppressed_because_sample_too_small") or {}),
        source_report=item.get("source_report"),
    )


def _fallback_current_swing() -> StrategyRow | None:
    name = "swing_backtest_verified_20260811_bf58807.json"
    payload = _read(REPORTS / name)
    if not payload:
        return None
    m = payload.get("metrics", {})
    return StrategyRow(
        key="current_swing", display_name="現行波段策略", status="frozen_satellite",
        period=("2015-01-05", "2026-07-31"),
        sharpe=m.get("sharpe"), mdd=m.get("nav_mdd"), ann_ret=m.get("ann_ret"),
        total_return=m.get("nav_total_ret"),
        benchmark_key="0050_total_return", benchmark_sharpe=m.get("sharpe_0050"),
        benchmark_mdd=m.get("mdd_0050"),
        curve_dir="reports/swing_backtest_curve", source_report=f"reports/{name}",
    )


def load_comparison() -> tuple[list[StrategyRow], dict, bool]:
    """回傳（策略列, meta, provisional）。

    meta 帶著權威檔的 ``definitions``／``purpose``／``curve_availability``，
    讓頁面能把「這些數字怎麼定義的」直接顯示給使用者，而不是藏在程式碼裡。
    """
    canonical = _read(CANONICAL)
    if canonical:
        rows = [_row_from_canonical(item) for item in canonical.get("strategies", [])]
        meta = {k: canonical.get(k) for k in
                ("definitions", "purpose", "curve_availability", "generated_by")}
        return rows, meta, False

    rows = [r for r in (_fallback_current_swing(),) if r]
    return rows, {}, True


def comparability_warnings(rows: list[StrategyRow]) -> list[str]:
    """把「這些數字為什麼不能直接比」講清楚，而不是讓表格假裝可比。"""
    notes: list[str] = []
    periods = {r.period for r in rows if r.period}
    if len(periods) > 1:
        notes.append(
            "**期間不同**：各策略的回測期間不重疊或只部分重疊，Sharpe 與年化報酬"
            "受各自期間的市場 régime 影響，**不可直接比大小**。")
    if any(r.status == "rejected_at_F1" for r in rows):
        notes.append(
            "**已否決的策略仍然列出**，是為了顯示判決結果，不是候選清單。"
            "不得據此回頭調整參數搶救（SPEC §9.5）；也不得從其失效形態產生新假說"
            "——一次性 holdout 用於探索即為污染。")
    suppressed = [r.display_name for r in rows if r.suppressed]
    if suppressed:
        notes.append(
            f"**{'、'.join(suppressed)} 的風險調整後指標已被抑制**："
            "樣本太小，年化與 Sharpe 不具統計意義，本頁不顯示數值。")
    inadequate = [r.display_name for r in rows if r.sample_is_adequate is False]
    if inadequate:
        notes.append(
            f"**樣本不足**：{'、'.join(inadequate)} 的樣本數少於 30。")
    return notes
