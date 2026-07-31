"""Resumable TWSE margin backfill into the configured database."""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_pipeline.fetchers.margin_fetcher import fetch_margin_twse_by_date, upsert_margin
from database.connection import get_session


def run(start: date, end: date, sleep: float = 0.2) -> tuple[int, int]:
    days = rows = 0
    with get_session() as s:
        resume = s.execute(text("SELECT MAX(trade_date) FROM margin_trading")).scalar()
    d = max(start, resume + timedelta(days=1)) if resume else start
    batch = []
    while d <= end:
        if d.weekday() < 5:
            df = fetch_margin_twse_by_date(d, retries=2)
            if not df.empty:
                batch.append(df); days += 1
            if len(batch) >= 25:
                rows += upsert_margin(pd.concat(batch, ignore_index=True)); batch.clear()
                print(f"margin backfill: {d} days={days} rows={rows}", flush=True)
            time.sleep(max(0, sleep))
        d += timedelta(days=1)
    if batch:
        rows += upsert_margin(pd.concat(batch, ignore_index=True))
    return days, rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", default=str(date.today()))
    ap.add_argument("--sleep", type=float, default=.2)
    a = ap.parse_args()
    result = run(date.fromisoformat(a.start), date.fromisoformat(a.end), a.sleep)
    print(f"completed days={result[0]} rows={result[1]}")
