"""
scripts/backtest_ab.py

SPEC_QUANT_UPGRADE.md P3：A/B比較不同STRATEGY設定的10年回測結果，
不解析印出的文字報告，直接讀 run_backtest() 回傳的 tdf.attrs（見 agent/backtest.py）。

資料只載入一次（_load(parquet_dir=...)），多個cfg變體共用，避免重複讀10年parquet。

用法：
    python scripts/backtest_ab.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from loguru import logger

from agent.backtest import _load, run_backtest
from agent.strategy import STRATEGY

PARQUET_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "research")


def _fmt(m):
    if not m:
        return "n/a"
    return (f"總報酬{m['nav_total_ret']*100:+.1f}%  Sharpe{m['sharpe']:.2f}  "
           f"回撤{m['nav_mdd']*100:.1f}%  Calmar{m['calmar']:.2f}  "
           f"勝率{m['net_win']*100:.1f}%  筆數{len(m['_tdf'])}")


def run_variant(name, cfg, data):
    # data 3個變體共用同一個dict（含快取的_rs20/_avg_turnover等）：這幾個變體只改
    # score_candidates()用的權重(w_rs/w_momentum/...)，不改因子計算本身依賴的cfg鍵
    # （new_entry_min_lots/turnover_avg_days/market_filter_stock皆相同），快取可放心共用。
    logger.info(f"=== 跑變體：{name} ===")
    tdf = run_backtest(cfg=cfg, data=data, quiet=True, parquet_dir=None)
    if tdf is None or tdf.empty:
        logger.warning(f"  {name}：無交易")
        return None
    a = dict(tdf.attrs)
    a["_tdf"] = tdf
    logger.info(f"  {name}：{_fmt(a)}  0050對照 Sharpe{a['sharpe_0050']:.2f} 回撤{a['mdd_0050']*100:.1f}%")
    return a


def main(variants: dict):
    logger.info("=== 載入10年本機歷史資料（各變體共用）===")
    data = _load(parquet_dir=PARQUET_DIR)

    results = {}
    for name, cfg in variants.items():
        results[name] = run_variant(name, cfg, data)

    logger.info("=== 彙總 ===")
    for name, r in results.items():
        logger.info(f"  {name:30s}  {_fmt(r)}")
    return results


# P1因子研究結論（10年IC，horizon=10~20日，貼近現行~15.5日平均持有期）：
#   rs20（w_rs）/mom60（w_momentum）在這個時間窗統計顯著負相關 → 大砍權重
#   rev_yoy 全時間窗最強（60日ICIR 0.60，10~20日ICIR 0.41~0.42）且單調 → 大幅升權
#   invest_new_entry/invest_streak 全時間窗穩定正向、時間尺度吻合 → 維持高權重
#   foreign_buy 只在5日內顯著、20日後迅速衰減（60日ICIR僅0.02）→ 略降權
#   stack_days 要60日以上才顯著（現行持有期跑不完）→ 溫和降權，不歸零（P3若拉長持有期可再評估拉高）
P1_REWEIGHT = {
    **STRATEGY,
    "w_rs": 0.3, "w_momentum": 0.3, "w_trend_stack": 0.8,
    "w_rev_yoy": 3.0, "w_invest_new_entry": 2.5, "w_foreign_buy": 1.0,
}
P1_REWEIGHT_AGGRESSIVE = {
    **P1_REWEIGHT,
    "w_rs": 0.0, "w_momentum": 0.0,
}

if __name__ == "__main__":
    main({
        "A_baseline(現行D組預設)": STRATEGY,
        "B_P1重新配權(溫和)": P1_REWEIGHT,
        "C_P1重新配權(rs20/mom60歸零)": P1_REWEIGHT_AGGRESSIVE,
    })
