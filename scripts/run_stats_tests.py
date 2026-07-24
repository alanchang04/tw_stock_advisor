"""
scripts/run_stats_tests.py

把 SPEC §4.4 的統計檢定套到現行策略上（§4.2 的 deflated Sharpe 一併算）。

n_trials 取自 research/EXPERIMENTS.md 的誠實計數（≳58），這是 deflation 的關鍵輸入
——「+328% 是從將近 60 個變體裡挑出來的」這件事必須反映在統計門檻上。

用法：
    py scripts/run_stats_tests.py                    # 全期（含硬污染區）
    py scripts/run_stats_tests.py --split development  # 只用 development（§4.1）
"""
from __future__ import annotations
import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.backtest import _load, run_backtest
from agent.strategy import STRATEGY
from research import data_splits as ds
from research.stats_tests import format_report, run_all

RESEARCH_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "research")
#: research/EXPERIMENTS.md 的誠實計數（保守下界）
N_TRIALS = 58


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default=None,
                    choices=["development", "validation", "holdout"],
                    help="只用某一段資料（§4.1）。不給＝全期，含硬污染區。")
    ap.add_argument("--trials", type=int, default=N_TRIALS)
    args = ap.parse_args()

    data = _load(parquet_dir=RESEARCH_DIR)
    t = run_backtest(cfg=STRATEGY, data=data, quiet=True, parquet_dir=None)
    nav = pd.Series(t.attrs["nav"]).sort_index()
    n50 = pd.Series(t.attrs["nav_0050"]).sort_index() if t.attrs.get("nav_0050") else None

    label = "全期（含硬污染區 2025-06~）"
    if args.split:
        # slice_dates 會自動記帳到 research/SPLIT_ACCESS_LOG.md（§4.1 的制度化）
        keep = set(ds.slice_dates(list(nav.index), args.split,
                                  purpose="scripts/run_stats_tests.py §4.4 統計檢定"))
        nav = nav[[d in keep for d in nav.index]]
        if n50 is not None:
            n50 = n50[[d in keep for d in n50.index]]
        lo, hi = ds.SPLITS[args.split]
        t = t[(pd.to_datetime(t["entry_date"]).dt.date >= lo)
              & (pd.to_datetime(t["entry_date"]).dt.date <= hi)]
        label = f"{args.split}（{lo} ~ {hi}）"
        if args.split == "holdout":
            print(ds.HOLDOUT_CAVEAT)

    print(f"\n資料範圍：{label}")
    print(f"交易筆數：{len(t)}　"
          f"總報酬：{(nav.iloc[-1]/nav.iloc[0]-1)*100:+.1f}%　"
          f"0050：{(n50.iloc[-1]/n50.iloc[0]-1)*100:+.1f}%" if n50 is not None else "")

    res = run_all(trade_returns=t["net_ret"].tolist(),
                  nav=nav.to_dict(),
                  nav_bench=n50.to_dict() if n50 is not None else None,
                  n_trials=args.trials,
                  position_weight=1.0 / STRATEGY.get("max_open", 10))
    print()
    print(format_report(res, mdd_actual=t.attrs.get("nav_mdd")))


if __name__ == "__main__":
    main()
