"""
scripts/run_holding_horizon_study.py

**P3-5：持有期 × 因子時間尺度**（SPEC §5.0 第一題的第二個方向）。

P1 §3.4 證明所有通過驗證的因子都在 60 日以上最強（rev_yoy 60日ICIR 0.60、
stack_days 60日 t=24.3、投信新進場 CAR 60日 +2.02%），而系統平均 39 天就出場。
過去一貫做法是「把長週期因子降權去遷就短持有期」，**從沒試過反過來**。

這一輪延續 P3-3 的 ⑤（固定持有 39 天、無出場規則、不擋新倉 → dev 年化超額 -0.18pp），
只把持有期往 60/90/120 天延伸，其餘完全不動——**隔離「進場評分 × 持有長度」這一個變數**。

停損/移動停利/死亡交叉全關（要看的是因子未受干擾的報酬，同 CAR 的邏輯），
所以這是**研究設計不是可交易策略**。若長持有明顯改善，下一步才做「加回實務停損」的版本。

依 §4.6 判決後紀律：只用 development。
"""
from __future__ import annotations
import argparse
import os
import sys

import pandas as pd
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.backtest import _load, run_backtest
from agent.strategy import STRATEGY
from research import data_splits as ds

RESEARCH_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "research")
_NEVER_TRAIL = [(9.99, 0.99)]
HOLDS = [20, 39, 60, 90, 120]

# 純「進場評分 × 固定持有」：拿掉所有會提早出場的東西，隔離持有長度這一個變數
PURE = {
    "market_filter_block_entries": False,
    "stop_loss": 0.99,
    "trail_tiers": _NEVER_TRAIL,
    "exit_on_death_cross": False,
    "bear_reenable_death_cross": False,
    "exit_below_ma20": False,
    "exit_below_ma5": False,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="development",
                    choices=["development", "validation", "holdout"])
    ap.add_argument("--realistic", action="store_true",
                    help="保留現行停損/出場，改用『最短持有期』下限而非固定持有")
    args = ap.parse_args()

    lo, hi = ds.SPLITS[args.split]
    if args.split != "development":
        ds.slice_dates([lo, hi], args.split,
                       purpose="scripts/run_holding_horizon_study.py P3-5 持有期研究")
        if args.split == "holdout":
            print(ds.HOLDOUT_CAVEAT)

    data = _load(parquet_dir=RESEARCH_DIR)
    rows = []
    for H in HOLDS:
        if args.realistic:
            cfg = {**STRATEGY, "min_hold_days": H}       # 需 strategy 支援；預設沒有 → 見下方註
        else:
            cfg = {**STRATEGY, **PURE, "max_hold_days": H}
        t = run_backtest(cfg=cfg, data=data, quiet=True, parquet_dir=None,
                         start_date=lo, end_date=hi)
        if t is None or t.empty:
            logger.warning(f"hold={H}：無交易")
            continue
        a = t.attrs
        nav = pd.Series(a["nav"]).sort_index()
        n50 = pd.Series(a["nav_0050"]).sort_index() if a.get("nav_0050") else None
        yrs = len(nav) / 252
        s_ann = (nav.iloc[-1] / nav.iloc[0]) ** (1 / yrs) - 1
        b_ann = ((n50.iloc[-1] / n50.iloc[0]) ** (1 / yrs) - 1) if n50 is not None else float("nan")
        r = t["net_ret"]
        w, l = r[r > 0], r[r <= 0]
        rows.append({
            "hold": H, "年化": s_ann, "年化超額": s_ann - b_ann, "Sharpe": a["sharpe"],
            "回撤": a["nav_mdd"], "筆數": len(t), "勝率": a["net_win"],
            "實際持有": t["hold"].mean(),
            "賺賠比": abs(w.mean() / l.mean()) if len(w) and len(l) else float("nan"),
        })
        logger.info(f"done hold={H}：年化超額 {(s_ann-b_ann)*100:+.2f}pp")

    df = pd.DataFrame(rows)
    mode = "realistic（保留停損，加最短持有下限）" if args.realistic else "pure（無停損無出場，固定持有）"
    print(f"\n{'='*92}")
    print(f"P3-5 持有期研究　{args.split}（{lo} ~ {hi}）　{mode}　0050 年化 "
          f"{(df['年化'].iloc[0]-df['年化超額'].iloc[0])*100:.2f}%")
    print("=" * 92)
    print(f"  {'固定持有':>8}{'實際持有':>9}{'年化':>8}{'年化超額':>10}{'Sharpe':>8}"
          f"{'回撤':>8}{'筆數':>6}{'勝率':>7}{'賺賠比':>7}")
    print("  " + "-" * 80)
    for _, x in df.iterrows():
        print(f"  {int(x['hold']):>6}天{x['實際持有']:>7.1f}天{x['年化']*100:>7.2f}%"
              f"{x['年化超額']*100:>+9.2f}pp{x['Sharpe']:>8.2f}{x['回撤']*100:>7.1f}%"
              f"{int(x['筆數']):>6}{x['勝率']*100:>6.1f}%{x['賺賠比']:>7.2f}")
    print("  " + "-" * 80)
    print("\n  假設：因子在 60日+ 最強 → 持有期拉長，年化超額應上升（至少到 60~90 天）。")
    print("  若年化超額隨持有期**不升反降** → 時間尺度假說否決；若在 60~90 天見頂 → 成立。")
    print("  ⚠️ pure 版無停損，回撤會放大；這是研究設計不是可交易策略。")


if __name__ == "__main__":
    main()
