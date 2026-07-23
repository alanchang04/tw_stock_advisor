"""
scripts/backfill_margin_local.py

融資融券 10 年回補 → 本機 SQLite（2026-07-23）。

**一律走本機，不碰 Neon**：10 年逐日回補的量會把 Neon 的資料傳輸配額燒穿
（2026-07-17 真實事故，見 data_pipeline/local_research_db.py 開頭），研究資料本來
就該養在本機。回補完匯出 parquet 給 research/factor_lab 做 IC 研究。

用法：
    python scripts/backfill_margin_local.py --years 10
    python scripts/backfill_margin_local.py --years 10 --export   # 完成後匯出 parquet

支援續傳：進度記在 backfill_progress(task='margin')，中途中斷再跑會接續。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

from data_pipeline.fetchers.margin_fetcher import fetch_margin_twse_by_date
from data_pipeline.local_research_db import (
    ensure_local_tables, get_local_conn, get_progress, set_progress, upsert_df,
)

TASK = "margin"
POLITE_SLEEP = 2.5      # 對官方站台的禮貌間隔（秒）


def backfill(years: int = 10, end: date | None = None, sleep: float = POLITE_SLEEP) -> int:
    conn = get_local_conn()
    ensure_local_tables(conn)

    end = end or date.today()
    start = end - timedelta(days=365 * years)
    resume = get_progress(conn, TASK)
    if resume:
        start = max(start, resume + timedelta(days=1))
        logger.info(f"接續上次進度，從 {start} 開始")

    if start > end:
        logger.info("已是最新，無需回補")
        return 0

    logger.info(f"=== 融資融券回補（本機）：{start} ~ {end} ===")
    d, days_done, rows_total, empty_streak = start, 0, 0, 0
    t0 = time.time()
    while d <= end:
        if d.weekday() >= 5:                      # 六日跳過
            d += timedelta(days=1)
            continue
        df = fetch_margin_twse_by_date(d)
        if df.empty:
            empty_streak += 1
        else:
            empty_streak = 0
            df["trade_date"] = df["trade_date"].astype(str)
            upsert_df(conn, "margin_trading", df, date_cols=())
            rows_total += len(df)
            days_done += 1
        set_progress(conn, TASK, d)
        if days_done and days_done % 20 == 0:
            el = time.time() - t0
            logger.info(f"  進度 {d}｜已完成 {days_done} 個交易日、{rows_total:,} 列"
                        f"｜耗時 {el/60:.1f} 分")
        d += timedelta(days=1)
        time.sleep(sleep)

    conn.commit()
    logger.info(f"=== 完成：{days_done} 個交易日、{rows_total:,} 列，耗時 {(time.time()-t0)/60:.1f} 分 ===")
    return rows_total


def export(out_dir: str = None):
    """匯出成 parquet 供 factor_lab 讀（跟其他研究資料同一個資料夾）。"""
    import pandas as pd
    out_dir = out_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                      "data", "research")
    os.makedirs(out_dir, exist_ok=True)
    conn = get_local_conn()
    df = pd.read_sql("SELECT * FROM margin_trading", conn)
    path = os.path.join(out_dir, "margin.parquet")
    df.to_parquet(path, index=False)
    logger.info(f"已匯出 {len(df):,} 列 → {path}")
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--sleep", type=float, default=POLITE_SLEEP)
    ap.add_argument("--export", action="store_true", help="回補後匯出 parquet")
    ap.add_argument("--export-only", action="store_true", help="只匯出，不回補")
    a = ap.parse_args()
    if a.export_only:
        export()
    else:
        backfill(years=a.years, sleep=a.sleep)
        if a.export:
            export()
