"""
scripts/regime_decomposition_report.py

使用者提出：與其只看10年總體數字，應該把10年切成牛市/空頭/盤整震盪分開比較，
才知道策略在哪種市況下真的贏0050、哪種市況下輸——不是被單一段極端多頭
（如2023~2025 AI供應鏈超級週期）主導整體結論。這是市場濾網壓力測試
（scripts/market_filter_stress_test.py，2018/2020/2022三個熊市年份）的系統化版本，
這次涵蓋全部10個完整年份，用同一套市況分類邏輯逐年跑。

市況分類規則（透明、可重現，用0050當年總報酬 + 空頭天數佔比判斷）：
    年報酬 > +15%              → 多頭
    年報酬 < -10%               → 空頭
    其餘                        → 盤整/震盪

用法：
    python scripts/regime_decomposition_report.py
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from loguru import logger

from agent.backtest import _load, run_backtest, split_adjust
from agent.strategy import STRATEGY

PARQUET_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "research")
YEARS = list(range(2015, 2027))   # 2026只有到7月，資料本來就到這裡為止，一併納入


def classify_regime(year_ret: float) -> str:
    if year_ret > 0.15:
        return "多頭"
    if year_ret < -0.10:
        return "空頭"
    return "盤整"


def main():
    logger.info("=== 載入10年本機歷史資料（各年份共用） ===")
    data = _load(parquet_dir=PARQUET_DIR)
    closes = data["prices"].pivot_table(index="trade_date", columns="stock_id", values="close")
    mkt = split_adjust(closes["0050"]).dropna() if "0050" in closes.columns else None

    rows = []
    for year in YEARS:
        start, end = date(year, 1, 1), date(year, 12, 31)
        if mkt is None:
            continue
        yr_prices = mkt[(mkt.index >= start) & (mkt.index <= end)]
        if len(yr_prices) < 20:
            continue
        year_ret = yr_prices.iloc[-1] / yr_prices.iloc[0] - 1
        regime = classify_regime(year_ret)

        t = run_backtest(cfg=STRATEGY, data=data, quiet=True, start_date=start, end_date=end)
        if t is None or t.empty:
            logger.warning(f"  {year}（{regime}，0050 {year_ret*100:+.1f}%）：策略無交易，跳過")
            continue
        a = t.attrs
        rows.append({
            "year": year, "regime": regime, "bench_0050_ret": year_ret,
            "strat_ret": a["nav_total_ret"], "strat_sharpe": a["sharpe"], "strat_mdd": a["nav_mdd"],
            "sharpe_0050": a.get("sharpe_0050"), "mdd_0050": a.get("mdd_0050"), "n_trades": len(t),
        })
        logger.info(f"  {year}（{regime}，0050 {year_ret*100:+.1f}%）：策略總報酬{a['nav_total_ret']*100:+.1f}%  "
                   f"Sharpe{a['sharpe']:.2f}  回撤{a['nav_mdd']*100:.1f}%  筆數{len(t)}")

    df = pd.DataFrame(rows)
    if df.empty:
        logger.error("沒有任何年份跑出結果"); return

    logger.info("=== 按市況分組彙總（平均） ===")
    for regime, g in df.groupby("regime"):
        logger.info(f"  {regime}（{len(g)}年：{sorted(g['year'].tolist())}）：\n"
                   f"    策略平均年報酬{g['strat_ret'].mean()*100:+.1f}%  平均Sharpe{g['strat_sharpe'].mean():.2f}  "
                   f"平均回撤{g['strat_mdd'].mean()*100:.1f}%\n"
                   f"    0050平均年報酬{g['bench_0050_ret'].mean()*100:+.1f}%  "
                   f"策略贏0050的年數：{(g['strat_ret'] > g['bench_0050_ret']).sum()}/{len(g)}")

    out_path = os.path.join(PARQUET_DIR, "regime_decomposition.csv")
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    logger.info(f"=== 逐年明細已存 {out_path} ===")


if __name__ == "__main__":
    main()
