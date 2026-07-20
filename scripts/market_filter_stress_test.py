"""
scripts/market_filter_stress_test.py

SPEC_QUANT_UPGRADE.md P3 決策4：市場濾網（0050 vs MA60 判多空）在真正的空頭年份
（2018 貿易戰下殺、2020 COVID崩盤、2022 升息熊市）壓力測試，而不是只在
2025~2026這種多頭窗口裡「幾乎沒觸發過」就假設它有用。

三組對照（每個年份各跑一次）：
  A 現行預設（market_filter=True, market_filter_block_entries=True：空頭不開新倉+死亡交叉保護）
  B 只加出場保護，不擋新倉（market_filter_block_entries=False）
  C 完全關閉市場濾網（market_filter=False，對照組——沒有濾網會有多差）

另外報告每年「空頭天數佔比」，用來驗證濾網在這些年份是否真的有被觸發
（而不是像13個月多頭窗那樣濾網形同虛設）。

用法：
    python scripts/market_filter_stress_test.py
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from loguru import logger

from agent.backtest import _load, run_backtest, split_adjust
from agent.strategy import STRATEGY

PARQUET_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "research")
STRESS_YEARS = [2018, 2020, 2022]

VARIANTS = {
    "A_現行(擋新倉+死叉保護)": STRATEGY,
    "B_只加出場保護(不擋新倉)": {**STRATEGY, "market_filter_block_entries": False},
    "C_完全關閉濾網": {**STRATEGY, "market_filter": False},
}


def _fmt(a):
    if not a:
        return "n/a（該年無交易或資料不足）"
    return (f"總報酬{a['nav_total_ret']*100:+.1f}%  Sharpe{a['sharpe']:.2f}  "
           f"回撤{a['nav_mdd']*100:.1f}%  0050同期{a['bench_0050']*100:+.1f}%  筆數{a['_n']}")


def _bear_day_ratio(data, year):
    closes = data["prices"].pivot_table(index="trade_date", columns="stock_id", values="close")
    mkt = split_adjust(closes["0050"]) if "0050" in closes.columns else None
    if mkt is None:
        return None
    bull = mkt >= mkt.rolling(60, min_periods=30).mean()
    yr = bull[(bull.index >= date(year, 1, 1)) & (bull.index <= date(year, 12, 31))]
    if yr.empty:
        return None
    return float((~yr.fillna(True)).mean())


def run_variant(name, cfg, data, start_date, end_date):
    tdf = run_backtest(cfg=cfg, data=data, quiet=True, parquet_dir=None,
                       start_date=start_date, end_date=end_date)
    if tdf is None or tdf.empty:
        return None
    a = dict(tdf.attrs)
    a["_n"] = len(tdf)
    return a


def main():
    logger.info("=== 載入10年本機歷史資料（各年份/變體共用）===")
    data = _load(parquet_dir=PARQUET_DIR)

    for year in STRESS_YEARS:
        bear_ratio = _bear_day_ratio(data, year)
        logger.info(f"########## {year} 年（空頭天數佔比 {bear_ratio*100:.0f}%）##########"
                   if bear_ratio is not None else f"########## {year} 年（空頭佔比：0050資料不足）##########")
        start_date, end_date = date(year, 1, 1), date(year, 12, 31)
        for name, cfg in VARIANTS.items():
            r = run_variant(name, cfg, data, start_date, end_date)
            logger.info(f"  {name:26s}  {_fmt(r)}")

    logger.info("=== 全部年份跑完 ===")


if __name__ == "__main__":
    main()
