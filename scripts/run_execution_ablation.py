"""
scripts/run_execution_ablation.py

**P3-3/P3-4：執行層消融梯**（SPEC §5.0 第一題最後一塊）。

P3-1 量到摩擦成本只解釋落差的 30%；P3-2 否決了集中度假說（集中反而是 edge 來源），
並把矛頭指向**執行層**：dev 上理論上限年化超額 +18.4%、扣成本後 +13.8%，
而現行策略實際是 -1.78% → **約 15pp 落在停損/出場/市場濾網**。

這道梯子把執行層元件逐一拆掉，量出各自代價：

    ① baseline（現行）
    ② 關掉 market_filter_block_entries    → 「空手不進場」的代價
    ③ 停損放寬到 -20%                      → 「停損砍斷」的代價
    ④ ②+③ 同時                            → 是否有交互作用
    ⑤ ④ + 固定持有 39 天、拿掉所有出場規則  → 逼近 P3-2 的理論上限

**⑤ 是自我檢查不只是變體**：如果拆到最後接不回 P3-2 的 +18.4%，
代表「執行層」這個歸因本身有問題，必須回頭檢討，不能硬套敘事。

依 §4.6 判決後的紀律：**只在 development（2015-01~2020-12）跑**。
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

#: 讓移動停利永不觸發（trail_tiers=[] 會被 `or` 判成 falsy 而退回預設，不能用空list）
_NEVER_TRAIL = [(9.99, 0.99)]

LADDER = [
    ("① baseline（現行）", {}),
    ("② 不擋新倉（market filter）", {"market_filter_block_entries": False}),
    ("③ 停損放寬到 -20%", {"stop_loss": 0.20}),
    ("④ ②+③", {"market_filter_block_entries": False, "stop_loss": 0.20}),
    ("⑤ ④+固定持有39天、無出場規則", {
        "market_filter_block_entries": False,
        "stop_loss": 0.99,                  # 實質關閉
        "trail_tiers": _NEVER_TRAIL,
        "exit_on_death_cross": False,
        "bear_reenable_death_cross": False,
        "max_hold_days": 39,
    }),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="development",
                    choices=["development", "validation", "holdout"])
    args = ap.parse_args()

    lo, hi = ds.SPLITS[args.split]
    if args.split != "development":
        # 走記帳入口（§4.1）——非 dev 的存取一定要留痕
        ds.slice_dates([lo, hi], args.split,
                       purpose="scripts/run_execution_ablation.py P3-3/P3-4 執行層消融")
        if args.split == "holdout":
            print(ds.HOLDOUT_CAVEAT)

    data = _load(parquet_dir=RESEARCH_DIR)
    rows = []
    for name, override in LADDER:
        cfg = {**STRATEGY, **override}
        t = run_backtest(cfg=cfg, data=data, quiet=True, parquet_dir=None,
                         start_date=lo, end_date=hi)
        if t is None or t.empty:
            logger.warning(f"{name}：無交易")
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
            "設定": name, "年化": s_ann, "年化超額": s_ann - b_ann,
            "Sharpe": a["sharpe"], "回撤": a["nav_mdd"],
            "筆數": len(t), "勝率": a["net_win"], "平均持有": t["hold"].mean(),
            "賺賠比": abs(w.mean() / l.mean()) if len(w) and len(l) else float("nan"),
        })
        logger.info(f"done {name}：年化超額 {(s_ann-b_ann)*100:+.2f}pp")

    df = pd.DataFrame(rows)
    print(f"\n{'='*96}")
    print(f"P3-3/P3-4 執行層消融梯　{args.split}（{lo} ~ {hi}）　0050 年化 "
          f"{(df['年化'].iloc[0]-df['年化超額'].iloc[0])*100:.2f}%")
    print("=" * 96)
    print(f"  {'設定':<28}{'年化':>8}{'年化超額':>10}{'Sharpe':>8}{'回撤':>8}"
          f"{'筆數':>6}{'勝率':>7}{'持有':>7}{'賺賠比':>7}")
    print("  " + "-" * 88)
    for _, x in df.iterrows():
        print(f"  {x['設定']:<28}{x['年化']*100:>7.2f}%{x['年化超額']*100:>+9.2f}pp"
              f"{x['Sharpe']:>8.2f}{x['回撤']*100:>7.1f}%{int(x['筆數']):>6}"
              f"{x['勝率']*100:>6.1f}%{x['平均持有']:>6.1f}天{x['賺賠比']:>7.2f}")
    print("  " + "-" * 88)

    base = df["年化超額"].iloc[0]
    print("\n  各元件的邊際代價（相對 baseline）：")
    for _, x in df.iloc[1:].iterrows():
        d = x["年化超額"] - base
        print(f"    {x['設定']:<30}{d*100:>+7.2f}pp"
              f"　{'← 拆掉它會變好＝它在扣分' if d > 0 else '← 拆掉它會變差＝它有貢獻'}")
    print("\n  ⚠️ 自我檢查：⑤ 應該要接近 P3-2 的理論上限（dev 年化超額 +13.8%，扣成本後）。")
    print("     若差很遠，代表「執行層」這個歸因本身有問題，必須回頭檢討，不得硬套敘事。")


if __name__ == "__main__":
    main()
