"""
SPEC_QUANT_UPGRADE §4.5 régime 切片為標準報告格式。

規格書原話：
    每次回測自動分段報告：多頭段/空頭段/盤整段（以大盤 MA60 上下+斜率定義），
    各段的報酬/回撤/勝率。**單一總數字從此不再是結論依據。**

**為什麼這條重要**：2026-07-24 的分年對照證實了一個乾淨到不像真的規律——
強多頭年（2019/2020/2021/2023/2024）策略全部大幅落後 0050（2024 差 52.7pp），
熊市平盤年（2015/2018/2022）全部勝出。**一個 +328% 的總數字把這件事完全藏起來了。**
把 régime 切片變成標準輸出，就是讓這種「總數字掩蓋結構」的情況不可能再發生。

régime 定義（照規格書：大盤 MA60 上下 + 斜率）：
    多頭 bull   收盤 ≥ MA60 且 MA60 斜率 > 0
    空頭 bear   收盤 <  MA60 且 MA60 斜率 < 0
    盤整 range  其餘（站上均線但均線下彎、跌破均線但均線上彎）

大盤代理沿用回測的 `market_filter_stock`（預設 0050），與出場規則的 régime 判定
同源，避免報告講的「多頭段」跟策略內部認定的不是同一件事。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

BULL, BEAR, RANGE = "多頭", "空頭", "盤整"
_ORDER = (BULL, RANGE, BEAR)


def classify_regimes(market_close, ma_window: int = 60,
                     slope_window: int = 20) -> pd.Series:
    """大盤收盤序列 → 逐日 régime 標籤。

    斜率用「MA60 相對 slope_window 日前的變化」判定，比逐日差分穩定得多
    （逐日差分會在均線走平時瘋狂跳動，把一段行情切成碎片）。
    """
    s = pd.Series(market_close).dropna().sort_index()
    ma = s.rolling(ma_window, min_periods=max(2, ma_window // 2)).mean()
    slope = ma - ma.shift(slope_window)
    out = pd.Series(RANGE, index=s.index, dtype=object)
    out[(s >= ma) & (slope > 0)] = BULL
    out[(s < ma) & (slope < 0)] = BEAR
    out[ma.isna()] = RANGE          # 暖身期資料不足，一律算盤整（不假裝知道）
    return out


def _mdd(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return float("nan")
    peak = np.maximum.accumulate(equity)
    return float((equity / peak - 1.0).min())


def regime_metrics(nav, regimes, trades=None, nav_bench=None) -> pd.DataFrame:
    """
    逐 régime 的報酬／回撤／勝率／天數佔比，以及同期大盤對照。

    nav / nav_bench：dict{date: value} 或 Series（run_backtest 的 attrs 存 dict）。
    trades：回測交易表，需有 entry_date 與 net_ret；用進場日歸屬 régime。

    報酬算法：取該 régime 的所有交易日，把**日報酬連乘**——意義是「只在這種盤勢
    裡持有的話會賺多少」。這些日子不連續，所以不是可實現的績效，是歸因指標。
    """
    nav_s = pd.Series(nav).sort_index()
    reg = pd.Series(regimes).sort_index()
    idx = nav_s.index.intersection(reg.index)
    nav_s, reg = nav_s.loc[idx], reg.loc[idx]
    r = nav_s.pct_change()
    rb = pd.Series(nav_bench).sort_index().reindex(idx).pct_change() \
        if nav_bench is not None else None

    tr = None
    if trades is not None and len(trades) and "entry_date" in trades:
        tr = trades.copy()
        tr["_reg"] = pd.to_datetime(tr["entry_date"]).dt.date.map(
            {(d.date() if hasattr(d, "date") else d): v for d, v in reg.items()})

    rows = []
    for name in _ORDER:
        mask = (reg == name)
        days = int(mask.sum())
        rr = r[mask].dropna()
        eq = (1.0 + rr).cumprod().to_numpy()
        row = {
            "régime": name,
            "交易日數": days,
            "天數佔比": days / len(reg) if len(reg) else np.nan,
            "累積報酬": float(eq[-1] - 1.0) if len(eq) else np.nan,
            "區間內回撤": _mdd(eq),
        }
        if rb is not None:
            eb = (1.0 + rb[mask].dropna()).cumprod().to_numpy()
            row["0050同期"] = float(eb[-1] - 1.0) if len(eb) else np.nan
            row["超額"] = row["累積報酬"] - row["0050同期"] \
                if not (np.isnan(row["累積報酬"]) or np.isnan(row["0050同期"])) else np.nan
        if tr is not None:
            g = tr[tr["_reg"] == name]
            row["進場筆數"] = int(len(g))
            row["勝率"] = float((g["net_ret"] > 0).mean()) if len(g) else np.nan
            row["平均報酬"] = float(g["net_ret"].mean()) if len(g) else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def format_report(df: pd.DataFrame) -> str:
    """把 regime_metrics 排成報告文字（納入回測輸出用）。"""
    has_b = "0050同期" in df.columns
    has_t = "進場筆數" in df.columns
    L = ["─" * 78, "régime 切片（SPEC §4.5）——單一總數字不再是結論依據", "─" * 78,
         "  ⚠️ 報酬＝只取該盤勢的交易日連乘。這些日子不連續，**不是可實現績效**，",
         "     是「錢在什麼盤勢下賺到/賠掉」的歸因指標。策略與 0050 用同一套算法，可比。",
         "     「均報酬」則是以**進場日**歸屬盤勢的交易平均，跟上面問的是不同問題。"]
    head = f"  {'盤勢':<6}{'天數':>7}{'佔比':>7}{'策略報酬':>11}{'區間回撤':>10}"
    if has_b:
        head += f"{'0050同期':>11}{'超額':>10}"
    if has_t:
        head += f"{'進場':>6}{'勝率':>7}{'均報酬':>9}"
    L.append(head)
    L.append("  " + "-" * (len(head) - 2))
    for _, r in df.iterrows():
        line = (f"  {r['régime']:<6}{int(r['交易日數']):>7}{r['天數佔比']*100:>6.1f}%"
                f"{r['累積報酬']*100:>+10.1f}%{r['區間內回撤']*100:>9.1f}%")
        if has_b:
            line += f"{r['0050同期']*100:>+10.1f}%{r['超額']*100:>+9.1f}%"
        if has_t:
            wr = "  n/a" if pd.isna(r.get("勝率")) else f"{r['勝率']*100:>6.1f}%"
            ar = "    n/a" if pd.isna(r.get("平均報酬")) else f"{r['平均報酬']*100:>+8.1f}%"
            line += f"{int(r['進場筆數']):>6}{wr}{ar}"
        L.append(line)
    if has_b:
        best = df.loc[df["超額"].idxmax(), "régime"] if df["超額"].notna().any() else None
        worst = df.loc[df["超額"].idxmin(), "régime"] if df["超額"].notna().any() else None
        if best and worst and best != worst:
            L.append("")
            L.append(f"  → 相對 0050：**{best}段最強、{worst}段最弱**。"
                     f"總報酬單一數字會把這個結構完全藏起來。")
    L.append("─" * 78)
    return "\n".join(L)
