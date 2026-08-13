"""讀取已驗證回測的 NAV artifact，並計算平滑度診斷。

為什麼有這個模組
----------------
`reports/swing_backtest_verified_*.json` 只保存 NAV 的 **SHA-256**（那是驗證檔，
不是資料檔），所以畫不出權益曲線。`scripts/run_verified_backtest.py --curve-output`
會另存 NAV 序列與交易明細，本模組負責把它讀進來並算出判讀平滑度所需的衍生序列。

前端因此**完全不需要接觸任何原始價量**：原始快照有 480 萬列價格，
NAV 只有每日兩個數字，artifact 約 120 KB。

摘要統計會藏住形狀
------------------
`SPEC_DATA_FOUNDATION_AND_MOMENTUM.md` §1.1 記載現行策略最大四筆交易佔全期淨利
67.2%、2025 一年貢獻 80.7%。Sharpe 0.919 看起來普通，但那條曲線的真實形狀是
「長期平坦 + 一根大陽」。`cumulative_pnl_excluding_top` 就是為了把這件事畫出來。

範圍限制
--------
**只適用現行波段策略。** MOM-1／REV-1 的 NAV 一旦產出即等同開封 backward
holdout，是 release `usage_policy` 明文禁止的行為。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CURVE_DIR = ROOT / "reports/swing_backtest_curve"


@dataclass(frozen=True)
class BacktestCurve:
    nav: pd.DataFrame           # trade_date, strategy_nav, benchmark_nav?
    trades: pd.DataFrame
    provenance: dict

    @property
    def has_benchmark(self) -> bool:
        return "benchmark_nav" in self.nav.columns and self.nav["benchmark_nav"].notna().any()


def load_curve(curve_dir: str | Path = DEFAULT_CURVE_DIR) -> BacktestCurve:
    """讀取 artifact。缺檔直接 raise——前端該顯示「尚未產生」而不是畫一張空圖。"""
    curve_dir = Path(curve_dir)
    missing = [name for name in ("nav_curve.parquet", "trades.parquet", "provenance.json")
               if not (curve_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"缺少回測曲線 artifact: {missing}；請先執行 "
            "scripts/run_verified_backtest.py --curve-output reports/swing_backtest_curve"
        )
    nav = pd.read_parquet(curve_dir / "nav_curve.parquet")
    nav["trade_date"] = pd.to_datetime(nav["trade_date"])
    nav = nav.sort_values("trade_date").reset_index(drop=True)
    trades = pd.read_parquet(curve_dir / "trades.parquet")
    for column in ("entry_date", "exit_date"):
        if column in trades.columns:
            trades[column] = pd.to_datetime(trades[column])
    provenance = json.loads((curve_dir / "provenance.json").read_text(encoding="utf-8"))
    return BacktestCurve(nav=nav, trades=trades, provenance=provenance)


def drawdown(nav: pd.Series) -> pd.Series:
    """相對歷史高點的回撤（負值）。"""
    nav = pd.Series(nav, dtype=float)
    return nav / nav.cummax() - 1.0


def monthly_returns(nav: pd.DataFrame, column: str = "strategy_nav") -> pd.DataFrame:
    """年 × 月的月報酬矩陣，供熱圖使用。

    以月最後一個有值的交易日計算，不補月中缺漏。
    """
    series = nav.set_index("trade_date")[column].astype(float)
    month_end = series.resample("ME").last().dropna()
    returns = month_end.pct_change()
    # 第一個月以期初 NAV 為基準，否則會平白丟掉一個月。
    if len(month_end):
        returns.iloc[0] = month_end.iloc[0] / float(series.iloc[0]) - 1.0
    frame = returns.to_frame("ret")
    frame["year"] = frame.index.year
    frame["month"] = frame.index.month
    return frame.pivot_table(index="year", columns="month", values="ret")


def rolling_return(nav: pd.DataFrame, column: str, window_sessions: int = 252) -> pd.Series:
    """滾動 N 個交易日報酬。窗長不足的期間回傳 NaN，不以較短窗頂替。"""
    series = nav.set_index("trade_date")[column].astype(float)
    return series / series.shift(window_sessions) - 1.0


def cumulative_pnl_excluding_top(trades: pd.DataFrame, exclude_counts=(0, 5, 10)) -> pd.DataFrame:
    """累計已實現淨損益，以及移除獲利最高的前 N 筆之後的同一條曲線。

    刻意用**已實現淨損益**而不是 NAV：損益是可加的，移除某幾筆就是把它們的
    `net_pnl` 拿掉，結果精確。若改動 NAV 則必須重算複利路徑，那只能近似。

    這正是 §9.2 F1 門檻「移除最佳 5 筆後淨損益仍為正」所檢驗的東西。
    """
    if "net_pnl" not in trades.columns or "exit_date" not in trades.columns:
        raise ValueError("trades 需要 net_pnl 與 exit_date 欄位")
    ordered = trades.dropna(subset=["exit_date"]).sort_values("exit_date")
    rank = ordered["net_pnl"].rank(ascending=False, method="first")
    frames = {}
    for n in exclude_counts:
        kept = ordered[rank > n] if n else ordered
        cumulative = kept.groupby("exit_date")["net_pnl"].sum().cumsum()
        label = "完整" if n == 0 else f"移除最佳 {n} 筆"
        frames[label] = cumulative
    result = pd.DataFrame(frames).sort_index()
    return result.ffill().fillna(0.0)


def concentration_summary(trades: pd.DataFrame, top_n: int = 4) -> dict:
    """前 N 筆交易佔總淨利的比重——摘要統計藏住形狀時，這個數字會說話。"""
    pnl = trades["net_pnl"].astype(float)
    gains = pnl[pnl > 0]
    total_net = float(pnl.sum())
    top = gains.nlargest(top_n)
    return {
        "trades": int(len(pnl)),
        "total_net_pnl": total_net,
        "top_n": top_n,
        "top_n_pnl": float(top.sum()),
        "top_n_share_of_net": float(top.sum() / total_net) if total_net else None,
        "win_rate": float((pnl > 0).mean()) if len(pnl) else None,
    }
