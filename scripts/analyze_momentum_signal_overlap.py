"""現行策略價格因子 vs MOM-1 形成訊號的重疊度量測。

問題：MOM-1（SPEC_DATA_FOUNDATION_AND_MOMENTUM.md §7.2 的 mom_6_1）加進多策略組合
後，是不是只是把現行策略已經在做的事再做一次？

作法：在 2005~2014（現行策略從未看過的期間）逐月末計算四個純價格因子的橫斷面排名，
量測 mom_6_1 與其餘三者的 Spearman rank correlation 與前 10% 選股重疊率。

只量測「訊號重疊」，不計算任何策略報酬——本腳本不觸碰 MOM-1 績效，
不違反 SPEC §0「資料品質閘門未通過前禁止查看 MOM-1 績效」。

還原：使用 reports/twse_corporate_action_jump_audit_2005_2014_events.csv 的
adjustment_factor 做反向還原。這是 D3 稽核的衍生產物，不是已 promotion 的 D3 snapshot，
因此結論標記為 preliminary。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PRICES = ROOT / "data/research_versions/twse_prices_2005_2014_v1/prices.parquet"
EVENTS = ROOT / "reports/twse_corporate_action_jump_audit_2005_2014_events.csv"
MASTER = ROOT / "data/research_versions/twse_security_master_2005_2007_staging_v3/stocks.parquet"
OUT = ROOT / "reports/momentum_signal_overlap_2005_2014.json"

MIN_PRICE = 10.0            # SPEC §7.1.4
MIN_HISTORY = 252           # SPEC §7.1.3
LIQUIDITY_TOP_FRAC = 0.50   # SPEC §7.1.5
TOP_DECILE = 0.10           # SPEC §7.2 新進場取前 10%


def load_adjusted_close() -> pd.DataFrame:
    """回傳 wide 的還原收盤價（index=trade_date, columns=stock_id）。"""
    px = pd.read_parquet(PRICES, columns=["stock_id", "trade_date", "close", "turnover"])
    px["trade_date"] = pd.to_datetime(px["trade_date"])

    close = px.pivot(index="trade_date", columns="stock_id", values="close").sort_index()
    turnover = px.pivot(index="trade_date", columns="stock_id", values="turnover").sort_index()

    ev = pd.read_csv(EVENTS, usecols=["stock_id", "event_date", "adjustment_factor"])
    ev["stock_id"] = ev["stock_id"].astype(str).str.strip()
    ev["event_date"] = pd.to_datetime(ev["event_date"])
    ev = ev.dropna(subset=["adjustment_factor"])
    ev = ev[(ev["adjustment_factor"] > 0) & (ev["adjustment_factor"] <= 1.5)]

    # 反向還原：t 之後每有一次除權息／減資，t 當時的價格要乘上該事件的 adjustment_factor。
    factor = pd.DataFrame(1.0, index=close.index, columns=close.columns)
    applied = 0
    for sid, grp in ev.groupby("stock_id"):
        if sid not in factor.columns:
            continue
        col = pd.Series(1.0, index=close.index)
        for d, f in zip(grp["event_date"], grp["adjustment_factor"]):
            col.loc[col.index < d] *= f
            applied += 1
        factor[sid] = col
    adj = close * factor
    return adj, turnover, close, applied


def month_end_dates(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    s = pd.Series(index, index=index)
    return list(s.groupby([index.year, index.month]).max())


def consecutive_true(mask: pd.DataFrame) -> pd.DataFrame:
    """逐欄「至當列為止連續 True 的天數」，對齊 agent/strategy.py 的多頭排列定義。"""
    csum = mask.cumsum()
    reset = csum.where(~mask).ffill().fillna(0)
    return (csum - reset).where(mask, 0)


def main() -> None:
    adj, turnover, raw_close, applied = load_adjusted_close()

    ma5 = adj.rolling(5).mean()
    ma20 = adj.rolling(20).mean()
    ma60 = adj.rolling(60).mean()
    stack_days = consecutive_true((ma5 > ma20) & (ma20 > ma60))

    mom_6_1 = adj.shift(20) / adj.shift(120) - 1.0     # SPEC §7.2
    rs20 = adj / adj.shift(20) - 1.0                   # 現行 rs20（w_rs=0.0）
    mom60 = adj / adj.shift(60) - 1.0                  # 現行 mom60（w_momentum=0.0）

    history = adj.notna().cumsum()
    liq20 = turnover.rolling(20).mean()

    rows = []
    for d in month_end_dates(adj.index):
        eligible = (
            adj.loc[d].notna()
            & (raw_close.loc[d] >= MIN_PRICE)
            & (history.loc[d] >= MIN_HISTORY)
            & mom_6_1.loc[d].notna()
            & rs20.loc[d].notna()
            & mom60.loc[d].notna()
            & liq20.loc[d].notna()
            & ~adj.columns.str.startswith("00")        # 排除 ETF 代號段（近似，非 PIT）
        )
        ids = adj.columns[eligible]
        if len(ids) < 50:
            continue
        liq = liq20.loc[d, ids]
        ids = liq[liq >= liq.quantile(1 - LIQUIDITY_TOP_FRAC)].index
        if len(ids) < 50:
            continue

        frame = pd.DataFrame({
            "mom_6_1": mom_6_1.loc[d, ids],
            "rs20": rs20.loc[d, ids],
            "mom60": mom60.loc[d, ids],
            "stack_days": stack_days.loc[d, ids],
        })
        ranks = frame.rank(pct=True)
        n_top = max(1, int(round(len(ids) * TOP_DECILE)))
        top_mom = set(frame["mom_6_1"].nlargest(n_top).index)
        row = {"date": d.date().isoformat(), "n": int(len(ids))}
        for other in ("rs20", "mom60", "stack_days"):
            row[f"spearman_{other}"] = float(ranks["mom_6_1"].corr(ranks[other]))
            top_other = set(frame[other].nlargest(n_top).index)
            row[f"top10_overlap_{other}"] = len(top_mom & top_other) / n_top
        rows.append(row)

    monthly = pd.DataFrame(rows)
    summary = {
        "period": [monthly["date"].min(), monthly["date"].max()],
        "months": int(len(monthly)),
        "median_universe_size": float(monthly["n"].median()),
        "corporate_action_adjustments_applied": applied,
        "adjustment_source": "reports/twse_corporate_action_jump_audit_2005_2014_events.csv",
        "caveat": "D3 未 promotion；還原係數取自稽核衍生檔，結論為 preliminary",
        "metrics": {},
    }
    for other in ("rs20", "mom60", "stack_days"):
        sp = monthly[f"spearman_{other}"]
        ov = monthly[f"top10_overlap_{other}"]
        summary["metrics"][other] = {
            "spearman_mean": round(float(sp.mean()), 4),
            "spearman_median": round(float(sp.median()), 4),
            "spearman_std": round(float(sp.std()), 4),
            "spearman_p05": round(float(sp.quantile(0.05)), 4),
            "spearman_p95": round(float(sp.quantile(0.95)), 4),
            "months_negative": int((sp < 0).sum()),
            "top10_overlap_mean": round(float(ov.mean()), 4),
            "top10_overlap_median": round(float(ov.median()), 4),
        }
    OUT.write_text(json.dumps({"summary": summary, "monthly": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
