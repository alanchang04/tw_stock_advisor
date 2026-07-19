"""
scripts/run_factor_report.py

SPEC_QUANT_UPGRADE.md P1：對現行系統的每個因子跑 IC/十分位數分析 + 核心 edge 的事件研究。
用本機 10 年 parquet 資料（scripts/historical_backfill_local.py 的產出），不需要 DB。

跑很久（十分位數分析逐日迴圈，多因子×多時間窗），設計成印出進度、結果落地成 JSON，
方便中途查看/中斷後不用整個重跑。

用法：
    python scripts/run_factor_report.py --out data/research/factor_report.json
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from loguru import logger

from agent.backtest import _load_parquet, _available_rev_month
from agent.strategy import (
    compute_factor_matrices, compute_new_entry_flag, apply_total_return_adjustment,
)
from research.factor_lab import factor_report, event_car


def build_factor_matrices(data: dict) -> dict:
    """從 _load_parquet() 的資料重建各因子的「日期×股票」矩陣（跟 backtest.py 共用同一套計算，
    確保這裡驗證的因子，就是 STRATEGY 實際在用的那個因子，不是另外定義了一份）。"""
    closes = data["prices"].pivot_table(index="trade_date", columns="stock_id", values="close")
    closes = closes.where(closes > 0)
    div = data.get("dividends")
    if div is not None and not div.empty:
        logger.info("套用個股除權息還原...")
        closes = apply_total_return_adjustment(closes, div)

    tech = data["tech"]
    ma5p = tech.pivot_table(index="trade_date", columns="stock_id", values="ma5").reindex(
        index=closes.index, columns=closes.columns)
    ma20p = tech.pivot_table(index="trade_date", columns="stock_id", values="ma20").reindex(
        index=closes.index, columns=closes.columns)
    ma60p = tech.pivot_table(index="trade_date", columns="stock_id", values="ma60").reindex(
        index=closes.index, columns=closes.columns)

    inst = data["inst"]
    invest = inst.pivot_table(index="trade_date", columns="stock_id", values="invest_net").reindex(
        index=closes.index, columns=closes.columns)
    foreign = inst.pivot_table(index="trade_date", columns="stock_id", values="foreign_net").reindex(
        index=closes.index, columns=closes.columns)

    logger.info("計算 rs20 / stack_days / invest_streak（跟 strategy.compute_factor_matrices 同一份邏輯）...")
    rs20, stack_days, inv_streak = compute_factor_matrices(closes, ma5p, ma20p, ma60p, invest)
    invest_new_entry = compute_new_entry_flag(invest, min_lots=50)
    mom60 = closes / closes.shift(60) - 1
    foreign_buy = (foreign > 0).astype(float)

    rev_map = data.get("rev_map") or {}
    if rev_map:
        logger.info("計算 rev_yoy（point-in-time，跟正式選股用同一個 _available_rev_month 邏輯："
                    "M月營收於M+1月10日後才「可得」，事前不能偷看）...")
        rev_records = [{"stock_id": sid, "year_month": ym, "yoy_pct": yoy}
                       for (sid, ym), yoy in rev_map.items()]
        rev_pivot = pd.DataFrame(rev_records).pivot_table(
            index="year_month", columns="stock_id", values="yoy_pct")
        avail_ym = [_available_rev_month(d) for d in closes.index]
        rev_yoy = rev_pivot.reindex(avail_ym)
        rev_yoy.index = closes.index
        rev_yoy = rev_yoy.reindex(columns=closes.columns)
    else:
        logger.warning("沒有月營收資料（monthly_revenue.parquet 缺或空），rev_yoy 因子跳過")
        rev_yoy = None

    out = {
        "closes": closes,
        "rs20": rs20,
        "stack_days": stack_days.astype(float),
        "mom60": mom60,
        "invest_streak": inv_streak.astype(float),
        "invest_new_entry": invest_new_entry.astype(float),
        "foreign_buy": foreign_buy,
    }
    if rev_yoy is not None:
        out["rev_yoy"] = rev_yoy
    return out


def run(out_path: str, horizons=(5, 10, 20, 60)):
    t0 = time.time()
    logger.info("=== 載入10年本機歷史資料 ===")
    data = _load_parquet(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                      "data", "research"))
    mats = build_factor_matrices(data)
    closes = mats.pop("closes")

    results = {}
    for name, factor in mats.items():
        logger.info(f"=== 因子報告：{name} ===")
        t1 = time.time()
        results[name] = factor_report(name, factor, closes, horizons=list(horizons))
        logger.info(f"  {name} 完成，耗時 {(time.time()-t1)/60:.1f} 分鐘")
        # 每算完一個因子就落地一次，避免中途被中斷整個白跑
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"factors": results, "status": "partial"}, f, ensure_ascii=False, indent=1, default=str)

    # 核心 edge 事件研究：投信新進場（invest_new_entry）觸發後的CAR曲線，
    # 大盤基準用等權平均日報酬（沒有特別挑0050，避免0050本身的split_adjust/而非
    # total_return_adjust沒對齊造成偏誤；等權平均是回測既有的bench_eqw同款邏輯）。
    logger.info("=== 核心edge事件研究：投信新進場 CAR 曲線 ===")
    bench_ret = closes.pct_change().mean(axis=1)
    event_mask = (mats["invest_new_entry"] > 0)
    car = event_car(closes, event_mask, bench_ret, horizons=[1, 3, 5, 10, 20, 40, 60])
    results["_event_study_invest_new_entry"] = car.to_dict("index")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"factors": results, "status": "complete"}, f, ensure_ascii=False, indent=1, default=str)

    elapsed = (time.time() - t0) / 60
    logger.info(f"=== 全部完成，總耗時 {elapsed:.1f} 分鐘，結果 → {out_path} ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "research", "factor_report.json"))
    a = ap.parse_args()
    run(a.out)
