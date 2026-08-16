"""策略比較頁的資料層。

原則
----
**前端不重算任何指標。** 每個數字都直接取自已提交的研究報告，並帶著
``source_report`` 指回出處。若在這裡自己算一次 Sharpe，就會產生第二個真相，
之後對不上時沒人知道該信哪個。

**優先讀正規化檔。** 研究機若提供 ``reports/strategy_comparison_metrics.json``
（見 reports/REQUEST_2026-08-16_STRATEGY_COMPARISON_ARTIFACTS.md），一律以它為準；
沒有時才用下面的 adapter 從各自的原始報告取值，並標記 ``provisional=True``。

刻意不做的事
------------
- 不把不同定義的數字塞進同一欄。三套策略的「勝率」根本不是同一件事：
  現行策略是逐筆交易勝率、MOM-1 記的是月勝率、margin reversal 是 6 筆交易的比例。
  因此 ``win_rate`` 一律附帶 ``win_rate_basis``，由前端分開顯示。
- 不計算跨策略的排名或綜合評分。期間、régime、樣本單位都不同，
  排名會製造一個不存在的可比性。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
CANONICAL = REPORTS / "strategy_comparison_metrics.json"

#: 判決狀態 → (顯示文字, 是否為可部署候選)
STATUS_LABELS: dict[str, tuple[str, bool]] = {
    "frozen_satellite": ("凍結・衛星", False),
    "rejected_at_F1": ("F1 否決", False),
    "insufficient_sample": ("樣本不足", False),
    "not_executed": ("未執行", False),
    "candidate": ("候選", True),
}


@dataclass
class StrategyRow:
    key: str
    display_name: str
    status: str
    period: tuple[str, str] | None = None
    calendar_years: float | None = None
    factors: list[str] = field(default_factory=list)
    total_return: float | None = None
    cagr: float | None = None
    sharpe: float | None = None
    mdd: float | None = None
    calmar: float | None = None
    volatility: float | None = None
    win_rate: float | None = None
    win_rate_basis: str | None = None
    sample_n: int | None = None
    sample_unit: str | None = None
    benchmark_name: str | None = None
    benchmark_cagr: float | None = None
    benchmark_sharpe: float | None = None
    benchmark_mdd: float | None = None
    curve_dir: str | None = None
    source_report: str = ""
    note: str | None = None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, (self.status, False))[0]

    @property
    def has_curve(self) -> bool:
        return bool(self.curve_dir) and (ROOT / self.curve_dir).is_dir()


def _read(path: Path) -> dict | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _current_swing() -> StrategyRow | None:
    """現行波段策略：已驗證回測報告 + 曲線 artifact 的交易統計。"""
    name = "swing_backtest_verified_20260811_bf58807.json"
    payload = _read(REPORTS / name)
    if not payload:
        return None
    m = payload.get("metrics", {})
    years = m.get("calendar_years")

    # 勝率取自曲線 artifact 的交易明細，與「🔄 歷史績效」頁同一個函式，
    # 不是這裡另外算的——避免同一個數字在兩頁不一致。
    win_rate = sample_n = None
    try:
        from agent.backtest_curve import concentration_summary, load_curve
        curve = load_curve()
        summary = concentration_summary(curve.trades)
        win_rate, sample_n = summary["win_rate"], summary["trades"]
    except Exception:
        pass

    return StrategyRow(
        key="current_swing", display_name="現行波段策略", status="frozen_satellite",
        period=("2015-01-05", "2026-07-31"), calendar_years=years,
        factors=["投信連買", "投信新進場", "月營收年增", "月營收加速",
                 "外資買超", "多頭排列", "ETF 加碼"],
        total_return=m.get("nav_total_ret"), cagr=m.get("ann_ret"),
        sharpe=m.get("sharpe"), mdd=m.get("nav_mdd"), calmar=m.get("calmar"),
        volatility=m.get("ann_vol"),
        win_rate=win_rate, win_rate_basis="逐筆交易", sample_n=sample_n,
        sample_unit="交易", benchmark_name="0050 total return",
        benchmark_cagr=None, benchmark_sharpe=m.get("sharpe_0050"),
        benchmark_mdd=m.get("mdd_0050"),
        curve_dir="reports/swing_backtest_curve", source_report=f"reports/{name}",
        note="SPEC §4.6 判決後凍結，不再調參；定位為衛星。",
    )


def _mom1_variants() -> list[StrategyRow]:
    """MOM-1A／1B：F1 backward holdout（已開封一次，結論為否決）。"""
    name = "mom1_f1_backward_holdout.json"
    payload = _read(REPORTS / name)
    if not payload:
        return []
    window = payload.get("holdout_window", {})
    bench = payload.get("benchmark", {}).get("core", {})
    rows = []
    for variant, result in (payload.get("results") or {}).items():
        core = result.get("core", {})
        monthly = result.get("monthly", {})
        rows.append(StrategyRow(
            key=f"mom1_{variant.split('-')[-1].lower()}",
            display_name=f"{variant}（中期動能）", status="rejected_at_F1",
            period=(window.get("start", ""), window.get("end", "")),
            calendar_years=None,
            factors=["mom_6_1"] + (["0050 MA200 曝險開關"] if variant.endswith("B") else []),
            total_return=core.get("total_return"), cagr=core.get("cagr"),
            sharpe=core.get("sharpe"), mdd=core.get("max_drawdown"),
            calmar=core.get("calmar"), volatility=core.get("annualised_volatility"),
            win_rate=monthly.get("monthly_win_rate"), win_rate_basis="月",
            sample_n=monthly.get("months"), sample_unit="月",
            benchmark_name="0050 total return",
            benchmark_cagr=bench.get("cagr"), benchmark_sharpe=bench.get("sharpe"),
            benchmark_mdd=bench.get("max_drawdown"),
            curve_dir="reports/mom1_f1_curve", source_report=f"reports/{name}",
            note="2008–2014 只開封一次，已正式否決；漲跌停與撮合制度與現在不同。",
        ))
    return rows


def _margin_reversal() -> StrategyRow | None:
    name = "margin_reversal_study.json"
    payload = _read(REPORTS / name)
    if not payload:
        return None
    m = payload.get("strategy_metrics", {})
    b = payload.get("benchmark_metrics", {})
    trade = payload.get("trade_summary", {})
    return StrategyRow(
        key="margin_reversal", display_name="融資反轉", status="insufficient_sample",
        period=(payload.get("start", ""), payload.get("end", "")),
        calendar_years=m.get("calendar_years"),
        factors=["市場壓力", "超賣", "融資洗盤", "基本面過濾", "法人過濾"],
        total_return=m.get("total"), cagr=m.get("ann_ret"), sharpe=m.get("sharpe"),
        mdd=m.get("mdd"), calmar=m.get("calmar"), volatility=m.get("ann_vol"),
        win_rate=trade.get("win_rate"), win_rate_basis="逐筆交易",
        sample_n=payload.get("trade_count"), sample_unit="交易",
        benchmark_name="同期基準", benchmark_cagr=b.get("ann_ret"),
        benchmark_sharpe=b.get("sharpe"), benchmark_mdd=b.get("mdd"),
        curve_dir=None, source_report=f"reports/{name}",
        note="僅 6 筆交易、0.33 個日曆年。年化與 Sharpe 在此樣本下不具統計意義。",
    )


def load_comparison() -> tuple[list[StrategyRow], bool]:
    """回傳（策略列, provisional）。

    provisional=True 代表數字是由本模組從各自原始報告取值拼出來的，
    尚未經研究機以 ``strategy_comparison_metrics.json`` 正規化。
    """
    canonical = _read(CANONICAL)
    if canonical:
        rows = [StrategyRow(**{k: v for k, v in item.items()
                               if k in StrategyRow.__dataclass_fields__})
                for item in canonical.get("strategies", [])]
        return rows, False

    rows: list[StrategyRow] = []
    current = _current_swing()
    if current:
        rows.append(current)
    rows.extend(_mom1_variants())
    margin = _margin_reversal()
    if margin:
        rows.append(margin)
    return rows, True


def comparability_warnings(rows: list[StrategyRow]) -> list[str]:
    """把「這些數字為什麼不能直接比」講清楚，而不是讓表格假裝可比。"""
    notes: list[str] = []
    periods = {r.period for r in rows if r.period}
    if len(periods) > 1:
        notes.append(
            "**期間不同**：各策略的回測期間不重疊或只部分重疊，"
            "Sharpe 與年化報酬受各自期間的市場 régime 影響，不可直接比大小。")
    bases = {r.win_rate_basis for r in rows if r.win_rate_basis}
    if len(bases) > 1:
        notes.append(
            f"**勝率定義不同**（{'、'.join(sorted(bases))}）："
            "月勝率與逐筆交易勝率不是同一件事，本頁分欄顯示，不合併。")
    small = [r.display_name for r in rows
             if r.sample_n is not None and r.sample_n < 30]
    if small:
        notes.append(
            f"**樣本不足**：{'、'.join(small)} 的樣本數少於 30，"
            "其年化與風險調整後指標僅供記錄，不足以支持任何結論。")
    if any(r.status == "rejected_at_F1" for r in rows):
        notes.append(
            "**已否決的策略仍然列出**，目的是從曲線與數字找出失敗原因、產生下一個假說；"
            "不得據此回頭調整參數搶救（SPEC §9.5）。")
    return notes
