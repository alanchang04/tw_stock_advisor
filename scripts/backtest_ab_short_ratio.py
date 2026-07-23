"""
scripts/backtest_ab_short_ratio.py

券資比（short_ratio）因子 10 年 A/B——IC 過關後的第二關。

背景：10年IC研究（scripts/run_margin_factor_report.py）測了5個融資融券衍生因子，
只有券資比有料（h20 |ICIR| 0.252、h60 0.372，優於現行仍在用的 foreign_buy 0.168
與 stack_days 0.148；IC為負＝低券資比後續報酬較好）。

但 **IC 是必要非充分條件**：ICIR加權那次（§5.4）就是 IC 證據看起來合理，
A/B 一跑總報酬反而掉 67pp。所以照同一套紀律，A/B 沒有明確勝出就不改預設。

用法：
    python scripts/backtest_ab_short_ratio.py
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
    return (f"總報酬{m['nav_total_ret']*100:+8.1f}%  Sharpe{m['sharpe']:5.2f}  "
            f"回撤{m['nav_mdd']*100:6.1f}%  Calmar{m['calmar']:5.2f}  "
            f"勝率{m['net_win']*100:4.1f}%  筆數{len(m['_tdf']):4d}")


def run_variant(name, cfg, data):
    logger.info(f"=== 跑變體：{name} ===")
    tdf = run_backtest(cfg=cfg, data=data, quiet=True, parquet_dir=None)
    if tdf is None or tdf.empty:
        logger.warning(f"  {name}：無交易")
        return None
    a = dict(tdf.attrs)
    a["_tdf"] = tdf
    logger.info(f"  {name}：{_fmt(a)}")
    return a


def main():
    logger.info("載入 10 年本機研究資料（含 margin.parquet）…")
    data = _load(parquet_dir=PARQUET_DIR)
    if data.get("margin") is None:
        logger.error("缺 margin.parquet；請先跑 "
                     "python scripts/backfill_margin_local.py --years 10 --export")
        return

    # 權重掃描：現行最大權重是 rev_yoy 3.0，券資比 ICIR 介於 invest_streak(0.31) 與
    # foreign_buy(0.17) 之間，故試 0.5~2.0 這個區間，兩端都不合理就代表沒用
    variants = [("A baseline（不含券資比）", {**STRATEGY, "w_short_ratio": 0.0})]
    for w in (0.5, 1.0, 1.5, 2.0):
        variants.append((f"B w_short_ratio={w}", {**STRATEGY, "w_short_ratio": w}))

    results = {name: run_variant(name, cfg, data) for name, cfg in variants}

    print("\n" + "=" * 100)
    print("券資比因子 A/B（10 年）")
    print("=" * 100)
    for name, m in results.items():
        print(f"{name:<26} {_fmt(m)}")
    print("=" * 100)

    base = results.get("A baseline（不含券資比）")
    if base:
        print("\n相對 baseline：")
        for name, m in results.items():
            if not m or name.startswith("A "):
                continue
            print(f"  {name:<24} 總報酬{(m['nav_total_ret']-base['nav_total_ret'])*100:+7.1f}pp  "
                  f"Sharpe{m['sharpe']-base['sharpe']:+.2f}  "
                  f"回撤{(m['nav_mdd']-base['nav_mdd'])*100:+.1f}pp  "
                  f"Calmar{m['calmar']-base['calmar']:+.2f}")
    print("\n※ 判準（同 §5.4）：Sharpe 與回撤不得變差，且總報酬或 Calmar 要有明確改善；"
          "\n   還要看改善是否在多個權重下穩定出現——只有單一權重贏＝八成是雜訊。")


if __name__ == "__main__":
    main()
