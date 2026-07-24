"""
scripts/backfill_disposition_local.py

處置/注意股回補（SPEC_QUANT_UPGRADE §2.4）。

**兩個目的地**：
  1. `data/research/disposition_events.parquet` —— 回測讀這份（研究一律走本機檔案，
     不燒 Neon 免費層的傳輸配額）
  2. Neon `disposition_events` / `notice_events` —— 即時選股讀這份（`--db` 才寫）

用法：
    py scripts/backfill_disposition_local.py              # 只寫 parquet（安全，預設）
    py scripts/backfill_disposition_local.py --db         # 同時寫進 Neon（給 live 用）
    py scripts/backfill_disposition_local.py --from 2020  # 指定起始年
"""
from __future__ import annotations
import argparse
import os
import sys
import time
from datetime import date

import pandas as pd
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_pipeline.fetchers.disposition_fetcher import fetch_notice, fetch_punish

RESEARCH_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "research")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start_year", type=int, default=2015)
    ap.add_argument("--to", dest="end_year", type=int, default=date.today().year)
    ap.add_argument("--db", action="store_true", help="同時寫進 Neon（給即時選股用）")
    ap.add_argument("--no-notice", action="store_true", help="只抓處置，不抓注意股")
    args = ap.parse_args()

    punish_all, notice_all = [], []
    for y in range(args.start_year, args.end_year + 1):
        lo, hi = date(y, 1, 1), date(y, 12, 31)
        try:
            d = fetch_punish(lo, hi)
            punish_all.append(d)
            logger.info(f"{y} 處置：{len(d)} 筆（4位數個股）")
        except Exception as e:
            logger.error(f"{y} 處置抓取失敗：{e}")
        time.sleep(1)
        if not args.no_notice:
            try:
                n = fetch_notice(lo, hi)
                notice_all.append(n)
                logger.info(f"{y} 注意：{len(n)} 筆（4位數個股）")
            except Exception as e:
                logger.error(f"{y} 注意抓取失敗：{e}")
            time.sleep(1)

    os.makedirs(RESEARCH_DIR, exist_ok=True)
    punish = pd.concat([d for d in punish_all if not d.empty], ignore_index=True) \
        if any(not d.empty for d in punish_all) else pd.DataFrame()
    if not punish.empty:
        punish = punish.drop_duplicates(subset=["stock_id", "start_date"], keep="last")
        p = os.path.join(RESEARCH_DIR, "disposition_events.parquet")
        punish.to_parquet(p, index=False)
        logger.info(f"✅ 處置 {len(punish)} 筆 → {p}"
                    f"（{punish['stock_id'].nunique()} 檔不重複）")

    notice = pd.concat([n for n in notice_all if not n.empty], ignore_index=True) \
        if any(not n.empty for n in notice_all) else pd.DataFrame()
    if not notice.empty:
        notice = notice.drop_duplicates(subset=["stock_id", "notice_date", "reason"], keep="last")
        p = os.path.join(RESEARCH_DIR, "notice_events.parquet")
        notice.to_parquet(p, index=False)
        logger.info(f"✅ 注意 {len(notice)} 筆 → {p}（目前只存不用，見 §2.4）")

    if args.db:
        from data_pipeline.fetchers.disposition_fetcher import _upsert, ensure_disposition_tables
        ensure_disposition_tables()
        if not punish.empty:
            logger.info(f"寫入 Neon disposition_events：{_upsert(punish, 'disposition_events', '(stock_id, start_date)')} 筆")
        if not notice.empty:
            logger.info(f"寫入 Neon notice_events：{_upsert(notice, 'notice_events', '(stock_id, notice_date, reason)')} 筆")


if __name__ == "__main__":
    main()
