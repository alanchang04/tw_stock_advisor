"""
scripts/run_margin_factor_report.py

融資融券因子 IC 研究（2026-07-23）——回答使用者的問題「策略是否該考慮融資融券」。

**紀律**：照 P1 的教訓（rs20/動能這種「聽起來合理」的因子，實測 h10~h20 的 IC 是
**負的**），任何新因子都必須先過 IC 檢驗，確認有預測力才談接進 STRATEGY。
這支腳本只做檢驗、不改任何策略設定。

測 5 個從融資融券衍生的因子（都是橫斷面、跟現有因子同一套 factor_lab 量法）：

  margin_bal_chg5    近5日融資餘額變化率。散戶槓桿在加還是在減。
                     實務界普遍當**反指標**（融資大增＝散戶追高、籌碼凌亂）。
  margin_bal_chg20   同上但20日，看中期趨勢。
  short_ratio        融券餘額/融資餘額（券資比）。高＝空方壓力大，也可能軋空。
  short_bal_chg5     近5日融券餘額變化率。
  margin_util_z      融資餘額相對自身近60日的位置（0~1）。衡量「目前槓桿處在
                     自己的歷史高檔還是低檔」，比絕對餘額可跨股比較。

註：IC 為正代表「因子值高→後續報酬高」。若融資增加真是反指標，
    margin_bal_chg 的 IC 應該是**負的**（那也是有用的資訊，取負號即可用）。

已知限制：資料只有上市(TWSE)，上櫃受本機憑證問題擋住（見 margin_fetcher docstring）。

用法：
    python scripts/run_margin_factor_report.py --out data/research/margin_factor_report.json
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from loguru import logger

from agent.backtest import _load_parquet
from agent.strategy import apply_total_return_adjustment
from research.factor_lab import factor_report

RESEARCH_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "research")


def build_margin_factors(closes: pd.DataFrame, margin: pd.DataFrame) -> dict:
    """把 margin.parquet 轉成「日期×股票」的因子矩陣，對齊 closes 的 index/columns。"""
    margin = margin.copy()
    # 日期型別要跟 closes.index 對齊才 reindex 得到——parquet 存的是字串，
    # _load_parquet() 會轉成 datetime.date，但別的呼叫端不一定。型別不合會安靜地
    # 全部變成 NaN（開發時實際踩過：因子全空但不報錯），所以這裡明確對齊。
    margin["trade_date"] = pd.to_datetime(margin["trade_date"]).dt.date
    if len(closes.index) and isinstance(closes.index[0], str):
        margin["trade_date"] = margin["trade_date"].astype(str)
    margin["stock_id"] = margin["stock_id"].astype(str)

    def piv(col):
        return (margin.pivot_table(index="trade_date", columns="stock_id", values=col)
                .reindex(index=closes.index, columns=closes.columns))

    m_bal = piv("margin_balance")
    s_bal = piv("short_balance")

    # 變化率：用比率而非絕對量，才能跨股比較（大型股融資餘額本來就大）
    def chg_rate(df, n):
        prev = df.shift(n)
        return ((df - prev) / prev.where(prev > 0)).replace([float("inf"), float("-inf")], pd.NA)

    # 融資餘額在自身近60日的相對位置（0~1）：衡量槓桿是在自己的高檔還低檔
    roll_min = m_bal.rolling(60, min_periods=20).min()
    roll_max = m_bal.rolling(60, min_periods=20).max()
    span = (roll_max - roll_min).where(lambda x: x > 0)
    margin_util = (m_bal - roll_min) / span

    return {
        "margin_bal_chg5": chg_rate(m_bal, 5),
        "margin_bal_chg20": chg_rate(m_bal, 20),
        "short_ratio": (s_bal / m_bal.where(m_bal > 0)),
        "short_bal_chg5": chg_rate(s_bal, 5),
        "margin_util_z": margin_util,
    }


def run(out_path: str, horizons=(5, 10, 20, 60)):
    t0 = time.time()
    margin_path = os.path.join(RESEARCH_DIR, "margin.parquet")
    if not os.path.exists(margin_path):
        logger.error(f"找不到 {margin_path}；請先跑 "
                     f"python scripts/backfill_margin_local.py --years 10 --export")
        return

    logger.info("=== 載入 10 年本機歷史資料 ===")
    data = _load_parquet(RESEARCH_DIR)
    closes = data["prices"].pivot_table(index="trade_date", columns="stock_id", values="close")
    closes = closes.where(closes > 0)
    div = data.get("dividends")
    if div is not None and not div.empty:
        logger.info("套用個股除權息還原（跟其他因子研究同一套處理）...")
        closes = apply_total_return_adjustment(closes, div)

    margin = pd.read_parquet(margin_path)
    logger.info(f"融資融券資料：{len(margin):,} 列，"
                f"{margin['trade_date'].min()} ~ {margin['trade_date'].max()}，"
                f"{margin['stock_id'].nunique()} 檔")

    mats = build_margin_factors(closes, margin)
    cover = {k: f"{v.notna().sum().sum():,}" for k, v in mats.items()}
    logger.info(f"各因子有效值數：{cover}")

    results = {}
    for name, factor in mats.items():
        logger.info(f"=== 因子報告：{name} ===")
        t1 = time.time()
        results[name] = factor_report(name, factor, closes, horizons=list(horizons))
        # 注意：記憶體裡 horizons 的鍵是「整數」，只有 json.dump 之後才變字串——
        # 用 "20" 取會拿到 None（開發時實際踩過，log 顯示 IC=None 但檔案裡有值）
        _h = results[name].get("horizons", {})
        ic20 = ((_h.get(20) or _h.get("20") or {}).get("ic", {}) or {})
        logger.info(f"  {name} 完成（h20 IC={ic20.get('ic_mean')} ICIR={ic20.get('icir')}），"
                    f"耗時 {(time.time()-t1)/60:.1f} 分")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"factors": results, "status": "partial"}, f,
                      ensure_ascii=False, indent=1, default=str)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"factors": results, "status": "complete"}, f,
                  ensure_ascii=False, indent=1, default=str)
    logger.info(f"=== 完成，耗時 {(time.time()-t0)/60:.1f} 分鐘 → {out_path} ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(RESEARCH_DIR, "margin_factor_report.json"))
    ap.add_argument("--horizons", default="5,10,20,60")
    a = ap.parse_args()
    run(a.out, horizons=tuple(int(x) for x in a.horizons.split(",")))
