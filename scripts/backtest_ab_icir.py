"""
scripts/backtest_ab_icir.py

SPEC_QUANT_UPGRADE.md §5 決策1：ICIR 加權標準化合成 vs 現行手調權重，10 年 A/B。

要驗的假說：現行 score_candidates 把幾個最強的因子做成**二元旗標**
（rev_yoy 只用 `>0`、`>20%`；foreign_net 只用 `>0`），等於「營收年增 +555% 跟
+21% 拿一樣的分」，把 IC 研究說有預測力的量級資訊丟掉了。改成
「權重∝ICIR × 橫斷面排序標準化」是否會更好？

紀律同 P0~P3：**A/B 沒有明確勝出就不改預設**（P3 的訊號品質縮手機制就是這樣被否決的）。

用法：
    python scripts/backtest_ab_icir.py
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
    a["_tdf"] = tdf                     # 交易明細另外掛上（attrs 本身不含），供筆數/後續分析用
    logger.info(f"  {name}：{_fmt(a)}")
    return a


def main():
    logger.info("載入 10 年本機研究資料（parquet）…")
    data = _load(parquet_dir=PARQUET_DIR)

    variants = [
        ("A 現行手調權重(baseline)", {**STRATEGY, "score_mode": "manual"}),
        ("B ICIR加權 h20", {**STRATEGY, "score_mode": "icir", "icir_horizon": 20}),
        ("C ICIR加權 h10", {**STRATEGY, "score_mode": "icir", "icir_horizon": 10}),
        ("D ICIR加權 h60", {**STRATEGY, "score_mode": "icir", "icir_horizon": 60}),
    ]

    results = {}
    for name, cfg in variants:
        results[name] = run_variant(name, cfg, data)

    print("\n" + "=" * 100)
    print("ICIR 加權標準化合成 vs 現行手調權重（10 年）")
    print("=" * 100)
    for name, m in results.items():
        print(f"{name:<26} {_fmt(m)}")
    print("=" * 100)

    base = results.get("A 現行手調權重(baseline)")
    if base:
        print("\n相對 baseline 的變化：")
        for name, m in results.items():
            if not m or name.startswith("A "):
                continue
            print(f"  {name:<24} 總報酬{(m['nav_total_ret']-base['nav_total_ret'])*100:+7.1f}pp  "
                  f"Sharpe{m['sharpe']-base['sharpe']:+.2f}  "
                  f"回撤{(m['nav_mdd']-base['nav_mdd'])*100:+.1f}pp  "
                  f"Calmar{m['calmar']-base['calmar']:+.2f}")
    print("\n※ 判準：要改預設，至少 Sharpe 與回撤不能變差，且總報酬或 Calmar 要有明確改善。")


if __name__ == "__main__":
    main()
