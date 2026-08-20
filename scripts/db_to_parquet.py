"""
scripts/db_to_parquet.py

把現有 Neon DB 的回測所需資料匯出成本機 parquet，供 `run_backtest(parquet_dir=...)` 讀取。
兩個用途：
  1. 測試 parquet 回測路徑（匯出現有 13 個月 → 從 parquet 跑，應與直接讀 DB 數字一致）。
  2. 當作「歷史資料 parquet schema 範本」——之後匯入老師的 10 年+資料，或用 TWSE 回補，
     只要湊出同樣欄位的這幾個檔案，回測就能直接吃。

用法：
    python scripts/db_to_parquet.py                  # 匯出到預設 data/history/
    python scripts/db_to_parquet.py --out /path/hist  # 指定輸出目錄

parquet schema（回測 _load_parquet 讀的欄位；institutional/technical/…皆選配，缺會優雅降級）：
    prices.parquet             stock_id, trade_date, close, volume, turnover, change_pct[, open, high, low]
    institutional.parquet      stock_id, trade_date, total_net, foreign_net, invest_net   （單位：股）
    technical.parquet          stock_id, trade_date, ma5, ma20, ma60, rsi14, macd_hist,
                               signal_ma_cross, signal_breakout   （沒有就回測時用收盤現算）
    stock_industry_map.parquet stock_id, industry_code
    industries.parquet         industry_code, name_zh
    monthly_revenue.parquet    stock_id, year_month, yoy_pct
    dividend_events.parquet    stock_id, ex_date, pre_close, ref_price
                               （SPEC_QUANT_UPGRADE.md P0-2，個股除權息還原用；沒有就
                               回測跳過還原，行為等同修正前）

用法（10年研究資料庫）：
    python scripts/db_to_parquet.py --out data/research
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from sqlalchemy import text
from loguru import logger
from database.connection import get_session

QUERIES = {
    "prices.parquet": """
        SELECT stock_id, trade_date, open, high, low, close, volume, turnover, change_pct
        FROM daily_prices""",
    "institutional.parquet": """
        SELECT stock_id, trade_date, total_net, foreign_net, invest_net
        FROM institutional_trading""",
    "technical.parquet": """
        SELECT stock_id, trade_date, ma5, ma20, ma60, rsi14, macd_hist,
               signal_ma_cross, signal_breakout
        FROM technical_indicators""",
    "stock_industry_map.parquet": "SELECT stock_id, industry_code FROM stock_industry_map",
    "industries.parquet": "SELECT code AS industry_code, name_zh FROM industries",
    "monthly_revenue.parquet": "SELECT stock_id, year_month, yoy_pct FROM monthly_revenue",
    "dividend_events.parquet": "SELECT stock_id, ex_date, pre_close, ref_price FROM dividend_events",
}


def export(out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    with get_session() as s:
        for fname, sql in QUERIES.items():
            try:
                df = pd.read_sql(text(sql), s.bind)
            except Exception as e:
                logger.warning(f"跳過 {fname}：{e}")
                continue
            path = os.path.join(out_dir, fname)
            df.to_parquet(path, index=False)
            logger.info(f"✅ {fname}: {len(df):>8} 列 → {path}")
    logger.info(f"匯出完成 → {out_dir}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "history"))
    a = ap.parse_args()
    export(a.out)
