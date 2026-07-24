"""
scripts/run_concentration_study.py

**P3-2：集中度代價 + edge 的理論上限**（SPEC §5.0 第一題第二塊）。

§5.0 的落差是：核心 edge CAR +8.50%/年，組合實際超額 -7.17%/年，差 15.67pp。
P3-1 量出摩擦成本只解釋 4.64pp（30%），**剩 11.03pp 不明**。三個嫌疑犯之一是
**集中度**——CAR 是 92,084 個事件的分散平均，而組合只持 10 檔。

**這支腳本問的問題**：
    「把執行面的一切拿掉（不停損、不出場、不擋新倉），純粹按分數等權買前 K 檔、
      抱 H 天，這個 edge 到底值多少？」

回答兩件事：
  1. **理論上限**——最理想執行下的超額報酬。若連這個都輸 0050，
     P3-3/P3-4 就不必做了，直接回到 §4.6。
  2. **集中度曲線**——K 從 5 檔放大到全部，超額怎麼變。若超額隨 K 放大而上升，
     代表集中度確實在傷害我們；若隨 K 放大而下降，代表**評分是有鑑別力的**，
     問題不在集中度而在執行。

**方法上的刻意選擇**：
  - 用**分數等權**、不做張數取整（要問的是 edge 的上限，不是可執行性）
  - 不扣交易成本（成本已由 P3-1 單獨量過，這裡要看的是毛 edge；混在一起會重複計算）
  - 對照基準是 0050 同期間報酬，**扣掉基準後才叫超額**
    （2026-07-24 犯過「拿原始報酬當 edge」的錯，見 §4.5 附帶更正）

**資料切分**：依 §4.6 判決後的紀律，**只用 development（2015-01~2020-12）**。
"""
from __future__ import annotations
import argparse
import os
import sys

import numpy as np
import pandas as pd
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.backtest import (_candidates_asof, _load, _precompute_factors,
                            apply_total_return_adjustment, split_adjust)
from agent.stock_selector import TURNOVER_AVG_DAYS
from agent.strategy import STRATEGY
from research import data_splits as ds

RESEARCH_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "research")
K_LIST = (5, 10, 20, 50, 100, 200, 99999)     # 99999 = 全部候選
TRADING_DAYS = 252


def _setup(data, cfg):
    """複製 run_backtest 進迴圈前的矩陣準備（_candidates_asof 需要這些）。"""
    closes = data["prices"].pivot_table(index="trade_date", columns="stock_id", values="close")
    closes = closes.where(closes > 0)
    div = data.get("dividends")
    if cfg.get("total_return_adjust", True) and div is not None and not div.empty:
        closes = apply_total_return_adjustment(closes, div)
    data["_closes"] = closes
    _precompute_factors(data, cfg)

    piv = data["prices"].pivot_table(index="trade_date", columns="stock_id", values="turnover")
    piv = piv.reindex(index=closes.index, columns=closes.columns)
    data["_avg_turnover"] = piv.rolling(cfg.get("turnover_avg_days", TURNOVER_AVG_DAYS),
                                        min_periods=1).mean()
    vol = data["prices"].pivot_table(index="trade_date", columns="stock_id", values="volume")
    data["_volume"] = vol.reindex(index=closes.index, columns=closes.columns)
    chg = data["prices"].pivot_table(index="trade_date", columns="stock_id", values="change_pct")
    data["_change_pct"] = chg.reindex(index=closes.index, columns=closes.columns)
    from agent.strategy import build_disposition_index
    data["_disposition_idx"] = build_disposition_index(data.get("disposition"))
    return closes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="development",
                    choices=["development", "validation", "holdout", "full"])
    ap.add_argument("--hold", type=int, nargs="+", default=[39, 60, 120],
                    help="持有交易日數（39＝現行實際平均，60＝因子最強的時間窗）")
    ap.add_argument("--step", type=int, default=20, help="每隔幾個交易日取樣一次")
    args = ap.parse_args()

    cfg = {**STRATEGY, "use_hot_sector_gate": False}
    data = _load(parquet_dir=RESEARCH_DIR)
    closes = _setup(data, cfg)
    dates = sorted(closes.index)

    if args.split == "full":
        sample_dates, label = dates, "全期（診斷用）"
    else:
        sample_dates = ds.slice_dates(
            dates, args.split,
            purpose=f"scripts/run_concentration_study.py P3-2 集中度研究 (hold={args.hold})")
        lo, hi = ds.SPLITS[args.split]
        label = f"{args.split}（{lo} ~ {hi}）"
        if args.split == "holdout":
            print(ds.HOLDOUT_CAVEAT)

    pos = {d: i for i, d in enumerate(dates)}
    mkt = split_adjust(closes["0050"]) if "0050" in closes.columns else None
    picks = sample_dates[::args.step]
    logger.info(f"取樣 {len(picks)} 個日期（{label}，每 {args.step} 交易日一次）")

    rows = []
    for n, d in enumerate(picks):
        if n % 20 == 0:
            logger.info(f"  {n}/{len(picks)} {d}")
        ids = _candidates_asof(data, d, [], top_n=99999, cfg=cfg)
        if not ids:
            continue
        i = pos[d]
        for H in args.hold:
            j = min(i + H, len(dates) - 1)
            if j <= i:
                continue
            fwd = (closes.loc[dates[j], ids] / closes.loc[dates[i], ids] - 1).dropna()
            if fwd.empty:
                continue
            m0, m1 = (mkt.get(dates[i]), mkt.get(dates[j])) if mkt is not None else (None, None)
            bench = (m1 / m0 - 1) if m0 and m1 else np.nan
            for K in K_LIST:
                sub = fwd.iloc[:min(K, len(fwd))]     # ids 已依分數排序
                rows.append({"date": d, "H": H, "K": min(K, len(fwd)), "K_label": K,
                             "n": len(sub), "ret": sub.mean(), "bench": bench,
                             "excess": sub.mean() - bench})
    df = pd.DataFrame(rows)
    if df.empty:
        logger.error("沒有任何取樣結果")
        return

    print(f"\n{'='*84}\nP3-2 集中度研究：{label}　取樣 {df['date'].nunique()} 個日期\n"
          f"（分數等權、不停損、不出場、不擋新倉、**不扣交易成本**——量的是 edge 毛上限）\n{'='*84}")
    for H in args.hold:
        g = df[df["H"] == H]
        print(f"\n▍持有 {H} 個交易日（年化係數 {TRADING_DAYS/H:.1f}）")
        print(f"  {'取前K檔':>9}{'實際檔數':>10}{'原始報酬':>11}{'0050同期':>11}"
              f"{'超額':>10}{'年化超額':>11}{'超額>0比例':>12}")
        print("  " + "-" * 72)
        for K in K_LIST:
            s = g[g["K_label"] == K]
            if s.empty:
                continue
            ann = (1 + s["excess"].mean()) ** (TRADING_DAYS / H) - 1
            klab = "全部" if K == 99999 else str(K)
            print(f"  {klab:>9}{s['K'].mean():>10.0f}{s['ret'].mean()*100:>10.2f}%"
                  f"{s['bench'].mean()*100:>10.2f}%{s['excess'].mean()*100:>9.2f}%"
                  f"{ann*100:>10.2f}%{(s['excess']>0).mean()*100:>11.1f}%")
    # §4.4 紀律：報告任何超額數字都要附信賴區間。這裡的取樣高度重疊
    # （step=20、hold=39 → 相鄰樣本共用近半視窗），有效獨立樣本遠少於總取樣數，
    # bootstrap 的區間仍會偏窄，**當成樂觀下界看**。
    from research.stats_tests import bootstrap_mean_ci
    print(f"\n{'='*84}")
    print("超額報酬的 95% bootstrap 信賴區間（§4.4）")
    print(f"  {'持有':>6}{'取前K檔':>9}{'每期超額':>11}{'95%CI下界':>12}{'95%CI上界':>12}{'顯著':>7}")
    print("  " + "-" * 57)
    for H in args.hold:
        for K in (5, 10, 20, 99999):
            s = df[(df["H"] == H) & (df["K_label"] == K)]["excess"].dropna()
            if len(s) < 3:
                continue
            ci = bootstrap_mean_ci(s.tolist())
            klab = "全部" if K == 99999 else str(K)
            print(f"  {H:>6}{klab:>9}{ci['mean']*100:>10.2f}%{ci['ci_low']*100:>11.2f}%"
                  f"{ci['ci_high']*100:>11.2f}%{'  ✅' if ci['significant'] else '  ❌':>7}")
    eff = max(1, int(len(picks) * args.step / max(args.hold)))
    print(f"  ⚠️ 取樣重疊嚴重：{len(picks)} 個取樣點，最長持有期下有效獨立樣本僅約 {eff} 個。")
    print("     上面的區間是**樂觀下界**，真實不確定性更大。")

    print(f"\n{'='*84}")
    print("判讀指引：")
    print("  · 超額隨 K 放大而**上升** → 集中度在傷害我們，分散化是解方")
    print("  · 超額隨 K 放大而**下降** → 評分有鑑別力，問題不在集中度而在執行面")
    print("  · 連 K=5 的年化超額都輸 0050（即 ≤0）→ edge 撐不起任何執行，回到 §4.6")
    print("  · 以上皆**未扣**交易成本；P3-1 量到摩擦成本為 4.64%/年，要自行扣掉再看")


if __name__ == "__main__":
    main()
