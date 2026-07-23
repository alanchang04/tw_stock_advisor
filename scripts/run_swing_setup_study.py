"""
scripts/run_swing_setup_study.py

波段進場型態「盤整量縮 → 帶量紅K突破」的 10 年事件研究 + 條件消融（2026-07-23）。

要回答三個問題：
  Q1 這個型態進場後真的有超額報酬嗎？（事件研究 CAR，跟 P1 對「投信新進場」
     做的同一套方法：事件後 N 日相對大盤等權平均的累積超額報酬）
  Q2 我加的那幾個條件（量縮基底/相對振幅/收盤上緣/突破箱頂/趨勢過濾）哪些真的有用？
     → 逐條關掉重跑，看 CAR 與事件數怎麼變（消融）
  Q3 每天實際會有幾檔符合？（使用者想要每日20檔，但這是「事件」不是「排名」，
     數量本來就會浮動——先量出真實分佈再決定 UI 怎麼呈現）

紀律同 §5.4/§5.5：這只是研究，不改任何預設。

用法：
    python scripts/run_swing_setup_study.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from loguru import logger

from agent.backtest import _load_parquet
from agent.strategy import (SWING_SETUP_CFG, apply_total_return_adjustment,
                            compute_swing_setup)
from research.factor_lab import event_car

RESEARCH_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "research")
HORIZONS = [1, 3, 5, 10, 20, 40, 60]


def build_panels(data: dict) -> dict:
    """把 parquet 攤成 compute_swing_setup 需要的「日期×股票」矩陣。"""
    pr = data["prices"]
    def piv(col):
        return pr.pivot_table(index="trade_date", columns="stock_id", values=col)
    closes = piv("close").where(lambda x: x > 0)
    panels = {
        "opens": piv("open"), "highs": piv("high"), "lows": piv("low"),
        "closes": closes, "volumes": piv("volume"),
    }
    tech = data["tech"]
    for k, col in (("ma20", "ma20"), ("ma60", "ma60")):
        panels[k] = tech.pivot_table(index="trade_date", columns="stock_id",
                                     values=col).reindex(index=closes.index,
                                                         columns=closes.columns)
    for k in ("opens", "highs", "lows", "volumes"):
        panels[k] = panels[k].reindex(index=closes.index, columns=closes.columns)
    # 報酬要用除權息還原後的收盤（跟其他研究一致），但型態偵測用原始價（K線型態
    # 看的是實際成交的那根K棒，還原後的價格不是當時盤面上的樣子）
    div = data.get("dividends")
    adj = closes
    if div is not None and not div.empty:
        adj = apply_total_return_adjustment(closes, div)
    panels["closes_adj"] = adj
    return panels


def study(panels: dict, cfg: dict, label: str) -> dict:
    mask = compute_swing_setup(panels["opens"], panels["highs"], panels["lows"],
                               panels["closes"], panels["volumes"],
                               panels["ma20"], panels["ma60"], cfg)
    n_events = int(mask.sum().sum())
    per_day = mask.sum(axis=1)
    adj = panels["closes_adj"]
    bench = adj.pct_change().mean(axis=1)
    car = event_car(adj, mask, bench, horizons=HORIZONS)
    out = {
        "label": label, "n_events": n_events,
        "events_per_day_mean": round(float(per_day.mean()), 2),
        "events_per_day_median": int(per_day.median()),
        "events_per_day_p90": int(per_day.quantile(0.9)),
        "days_with_zero": int((per_day == 0).sum()),
        "days_total": int(len(per_day)),
        "car": car.to_dict("index") if car is not None and not car.empty else {},
    }
    c20 = (out["car"].get(20) or out["car"].get("20") or {})
    logger.info(f"  {label}: 事件 {n_events:,} 次｜每日中位 {out['events_per_day_median']} 檔"
                f"｜CAR20 {c20.get('car_mean')}")
    return out


def main():
    t0 = time.time()
    logger.info("=== 載入 10 年本機資料 ===")
    data = _load_parquet(RESEARCH_DIR)
    panels = build_panels(data)

    results = []
    logger.info("=== 基準：全部條件開啟 ===")
    results.append(study(panels, SWING_SETUP_CFG, "全部條件"))

    # 消融：逐條關掉，看 CAR 掉多少（掉越多＝該條件越有用）
    ablations = [
        ("關掉:相對振幅收縮", {"contraction_ratio": 0}),
        ("關掉:盤整量縮", {"require_vol_dryup": False}),
        ("關掉:突破日帶量", {"breakout_vol_mult": 0}),
        ("關掉:突破箱頂", {"require_box_break": False}),
        ("關掉:收盤上緣", {"close_top_frac": 0}),
        ("關掉:趨勢過濾", {"require_trend": False}),
        ("只有使用者原構想(5日盤整+1.5倍量紅K)",
         {"contraction_ratio": 0, "require_vol_dryup": False, "require_box_break": False,
          "close_top_frac": 0, "require_trend": False}),
    ]
    logger.info("=== 條件消融 ===")
    for label, override in ablations:
        results.append(study(panels, {**SWING_SETUP_CFG, **override}, label))

    out_path = os.path.join(RESEARCH_DIR, "swing_setup_study.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1, default=str)

    print("\n" + "=" * 108)
    print("波段進場型態事件研究（10年）：事件後相對大盤的累積超額報酬 CAR%")
    print("=" * 108)
    hdr = f"{'設定':<34}{'事件數':>8}{'每日中位':>9}{'零檔天數':>9}"
    for h in (5, 10, 20, 40, 60):
        hdr += f"{'CAR'+str(h):>9}"
    print(hdr)
    print("-" * 108)
    for r in results:
        car = r["car"]
        line = (f"{r['label']:<34}{r['n_events']:>8,}{r['events_per_day_median']:>9}"
                f"{r['days_with_zero']:>9}")
        for h in (5, 10, 20, 40, 60):
            v = (car.get(h) or car.get(str(h)) or {}).get("car_mean")
            line += f"{(v*100 if v is not None else float('nan')):>8.2f}%"
        print(line)
    print("=" * 108)
    print(f"\n※ CAR＝事件後 N 日相對「全市場等權平均」的累積超額報酬。>0 才代表這個型態"
          f"真的有進場價值。\n※ 消融看法：關掉某條件後 CAR 明顯下降＝該條件有用；"
          f"沒變或反而變好＝該條件無用甚至有害。")
    logger.info(f"完成，耗時 {(time.time()-t0)/60:.1f} 分 → {out_path}")


if __name__ == "__main__":
    main()
