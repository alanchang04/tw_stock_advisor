"""
scripts/exit_rule_ablation.py

SPEC_QUANT_UPGRADE.md P3 決策2：12條出場規則在10年資料上做消融測試，每次拔掉一條
看邊際貢獻，預期收斂到3~4條（災難停損+趨勢出場+事件研究決定的時間出場）。

只測目前預設「開啟」的6條 pattern-based 出場規則（stop_loss/移動停利是核心風控，
不在消融候選內——SPEC§5原文已預期這兩條會留下）：
  exit_kd_macd / exit_on_death_cross / exit_swing_low /
  exit_body_break / exit_large_candle / exit_upper_wick

用法：
    python scripts/exit_rule_ablation.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from loguru import logger

from agent.backtest import _load, run_backtest
from agent.strategy import STRATEGY

PARQUET_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "research")

ABLATION_FLAGS = [
    "exit_kd_macd", "exit_on_death_cross", "exit_swing_low",
    "exit_body_break", "exit_large_candle", "exit_upper_wick",
]


def _fmt(a):
    if not a:
        return "n/a"
    return (f"總報酬{a['nav_total_ret']*100:+.1f}%  Sharpe{a['sharpe']:.2f}  "
           f"回撤{a['nav_mdd']*100:.1f}%  Calmar{a['calmar']:.2f}  "
           f"勝率{a['net_win']*100:.1f}%  筆數{a['_n']}")


def run_variant(name, cfg, data):
    logger.info(f"=== 跑變體：{name} ===")
    tdf = run_backtest(cfg=cfg, data=data, quiet=True, parquet_dir=None)
    if tdf is None or tdf.empty:
        logger.warning(f"  {name}：無交易")
        return None
    a = dict(tdf.attrs)
    a["_n"] = len(tdf)
    logger.info(f"  {name}：{_fmt(a)}")
    return a


def main():
    logger.info("=== 載入10年本機歷史資料（各變體共用）===")
    data = _load(parquet_dir=PARQUET_DIR)

    results = {}
    results["baseline(全部6條開啟)"] = run_variant("baseline(全部6條開啟)", STRATEGY, data)
    for flag in ABLATION_FLAGS:
        name = f"拔掉 {flag}"
        cfg = {**STRATEGY, flag: False}
        # exit_on_death_cross 有個容易漏踩的坑：STRATEGY["bear_reenable_death_cross"]=True
        # 會在熊市日用 bear_cfg 把死亡交叉強制重新打開（見 agent/backtest.py run_backtest
        # 的 bear_cfg 邏輯），只改 exit_on_death_cross 不會真的完全拔掉，熊市日仍會觸發。
        if flag == "exit_on_death_cross":
            cfg["bear_reenable_death_cross"] = False
        results[name] = run_variant(name, cfg, data)

    base = results["baseline(全部6條開啟)"]
    logger.info("=== 彙總（相對baseline的邊際貢獻，正值＝拔掉後變差＝這條規則有幫助）===")
    logger.info(f"  {'baseline':30s}  {_fmt(base)}")
    for flag in ABLATION_FLAGS:
        r = results[f"拔掉 {flag}"]
        if r is None or base is None:
            continue
        d_sharpe = base["sharpe"] - r["sharpe"]
        d_mdd = r["nav_mdd"] - base["nav_mdd"]   # 正值＝拔掉後回撤變深（變差）
        d_ret = base["nav_total_ret"] - r["nav_total_ret"]
        logger.info(f"  拔掉{flag:22s}  ΔSharpe={d_sharpe:+.3f}  Δ回撤={d_mdd*100:+.1f}pp  "
                   f"Δ總報酬={d_ret*100:+.1f}pp  |  {_fmt(r)}")
    return results


if __name__ == "__main__":
    main()
