"""
scripts/run_sympathy_study.py

**同族群補漲效應——事件研究（P3 新 edge 探索，2026-07-25）**

使用者假設：族群龍頭先動（國巨漲/財報好）→ 同族群「還沒動」的落後者（華新科）補漲。
機制假設＝資訊在同業間擴散（跟現有 edge「投信/營收擴散」同源，只是換一層）。

**結論：否決。** 兩種觸發都測（依 §4.6 紀律只用 development）：
  (a) 價格領先：補漲增量 +0.56%@20d，60日衰減到 +0.18%，扣成本 1.07% 後 ≈ 0 → 弱
  (b) 營收領先：補漲增量 **-1.26%@20d（負！）** → 落後者繼續落後，反向否決

關鍵發現：資訊擴散 edge 只在「個股自己的資訊→自己的價格」這層成立，
**不會擴散到同業**。族群裡「已經動的」後續繼續贏，「落後者落後是有原因的」。

⚠️ 次族群用公開產業結構手工建、只收流動性代表股、用現在成員（無 point-in-time）。
   絕對數字有存活股選樣偏誤；但「落後者 vs 已動者」是同宇宙內比較，相對結論穩健。

用法：
    py scripts/run_sympathy_study.py --trigger price     # (a) 價格領先
    py scripts/run_sympathy_study.py --trigger revenue    # (b) 營收領先
"""
from __future__ import annotations
import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.backtest import (_available_rev_month, _load, apply_total_return_adjustment,
                            split_adjust)
from research import data_splits as ds
from research.stats_tests import bootstrap_mean_ci

# 手工次族群（公開產業結構，只收流動性代表股，不挑贏家）
SUBSECTORS = {
    "被動元件": ["2327", "2492", "3026", "2375", "6173", "2456"],
    "矽晶圓":   ["6488", "5483", "6182", "3016", "3532"],
    "散熱":     ["3324", "3017", "6230", "2421", "3483"],
    "記憶體":   ["2408", "2344", "2337", "8299", "3260", "4967"],
    "IC設計":   ["2454", "3034", "2379", "4966", "6415", "3443", "5269"],
    "PCB":      ["3037", "3189", "8046", "2383", "2313", "6213"],
    "面板":     ["2409", "3481", "6116"],
    "光學鏡頭": ["3008", "3406", "3019"],
    "網通":     ["2345", "3596", "6285", "4906", "5388"],
    "重電":     ["1519", "1503", "1513", "1514", "1504"],
    "航運":     ["2603", "2609", "2615", "2606", "5608"],
    "鋼鐵":     ["2002", "2015", "2014", "2023", "2027", "2032"],
    "金融":     ["2881", "2882", "2891", "2886", "2884", "2892", "2880"],
    "汽車零組": ["1536", "1522", "2201", "2227", "3552"],
}
N = 20
HZ = [1, 3, 5, 10, 20, 40, 60]
PRICE_LEADER_MIN = 0.15    # (a) 領頭羊近 N 日 ≥ 15%
REV_LEADER_MIN = 20.0      # (b) 領頭羊營收年增 ≥ 20%
LAG_MAX = 0.05             # 落後者近 N 日價格漲幅 ≤ 5%
RESEARCH_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "research")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trigger", choices=["price", "revenue"], default="revenue")
    ap.add_argument("--split", default="development")
    ap.add_argument("--step", type=int, default=5)
    args = ap.parse_args()

    data = _load(parquet_dir=RESEARCH_DIR)
    closes = data["prices"].pivot_table(index="trade_date", columns="stock_id", values="close")
    closes = closes.where(closes > 0)
    div = data.get("dividends")
    if div is not None and not div.empty:
        closes = apply_total_return_adjustment(closes, div)
    dates = sorted(closes.index)
    pos = {d: i for i, d in enumerate(dates)}
    mkt = split_adjust(closes["0050"]).reindex(dates)
    rev_map = data.get("rev_map") or {}

    keep = set(ds.slice_dates(dates, args.split,
                              purpose=f"同族群補漲事件研究 trigger={args.trigger}"))
    sample = [d for d in dates[N:] if d in keep][::args.step]

    def fwd(sid, i, h):
        j = min(i + h, len(dates) - 1)
        v, v0 = closes.at[dates[j], sid], closes.at[dates[i], sid]
        return (v / v0 - 1) if pd.notna(v) and pd.notna(v0) and v0 > 0 else np.nan

    lag, ctrl, lead = ({h: [] for h in HZ} for _ in range(3))
    n_ev = 0
    for d in sample:
        i = pos[d]
        ym = _available_rev_month(d)
        fwd_mkt = {h: (mkt.iloc[min(i + h, len(dates) - 1)] / mkt.iloc[i] - 1) for h in HZ}
        for members in SUBSECTORS.values():
            valid = [s for s in members if s in closes.columns and pd.notna(closes.at[d, s])]
            if len(valid) < 3:
                continue
            if args.trigger == "price":
                score = {}
                for s in valid:
                    p0 = closes.at[dates[i - N], s]
                    if pd.notna(p0) and p0 > 0:
                        score[s] = closes.at[d, s] / p0 - 1
                if not score:
                    continue
                leader = max(score, key=score.get)
                if score[leader] < PRICE_LEADER_MIN:
                    continue
            else:
                score = {s: rev_map.get((s, ym)) for s in valid if rev_map.get((s, ym)) is not None}
                if not score:
                    continue
                leader = max(score, key=score.get)
                if score[leader] < REV_LEADER_MIN:
                    continue
            for h in HZ:
                v = fwd(leader, i, h)
                if not np.isnan(v):
                    lead[h].append(v - fwd_mkt[h])
            for s in valid:
                if s == leader:
                    continue
                p0 = closes.at[dates[i - N], s]
                if pd.isna(p0) or p0 <= 0:
                    continue
                is_lag = (closes.at[d, s] / p0 - 1) <= LAG_MAX
                bucket = lag if is_lag else ctrl
                if is_lag:
                    n_ev += 1
                for h in HZ:
                    v = fwd(s, i, h)
                    if not np.isnan(v):
                        bucket[h].append(v - fwd_mkt[h])

    tri = "價格領先(近%d日≥%d%%)" % (N, PRICE_LEADER_MIN * 100) if args.trigger == "price" \
        else "營收領先(年增≥%d%%)" % REV_LEADER_MIN
    print(f"\n{'='*80}\n同族群補漲事件研究　{args.split}　觸發＝{tri}　落後者近{N}日≤{LAG_MAX*100:.0f}%")
    print(f"落後者事件 {n_ev}\n{'='*80}")
    print(f"  {'水平':>6}{'落後者CAR':>12}{'matched對照':>13}{'補漲增量':>10}{'領頭羊CAR':>12}")
    print("  " + "-" * 55)
    for h in HZ:
        a = np.mean(lag[h]) if lag[h] else np.nan
        c = np.mean(ctrl[h]) if ctrl[h] else np.nan
        e = np.mean(lead[h]) if lead[h] else np.nan
        print(f"  +{h:>3}日{a*100:>11.2f}%{c*100:>12.2f}%{(a-c)*100:>9.2f}%{e*100:>11.2f}%")
    print("\n  95% bootstrap CI（落後者 vs matched 對照）：")
    for h in (20, 60):
        cl = bootstrap_mean_ci(lag[h])
        cc = bootstrap_mean_ci(ctrl[h])
        print(f"    +{h}日  落後者 {cl['mean']*100:+.2f}% [{cl['ci_low']*100:+.2f},{cl['ci_high']*100:+.2f}]"
              f"　對照 {cc['mean']*100:+.2f}% [{cc['ci_low']*100:+.2f},{cc['ci_high']*100:+.2f}]")
    print("\n  判讀：補漲增量（落後者−對照）明顯>0 且扣成本 1.07% 後仍正 → 補漲存在。")
    print("  ⚠️ 手工次族群、存活股宇宙，絕對值有偏誤；落後者vs對照為同宇宙比較，相對結論穩。")


if __name__ == "__main__":
    main()
