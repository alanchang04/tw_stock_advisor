"""Targeted repair for dates where prices exist but institutional rows are absent."""
from __future__ import annotations

import os
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_pipeline.fetchers.twse_fetcher import (
    _is_stock_code,
    fetch_institutional_tpex_by_date,
    fetch_institutional_twse,
)
from data_pipeline.local_research_db import ensure_local_tables, get_local_conn, upsert_df


def repair_institutional_gaps() -> dict[str, int]:
    conn = get_local_conn()
    ensure_local_tables(conn)
    gaps = [r[0] for r in conn.execute(
        "SELECT trade_date FROM daily_prices GROUP BY trade_date "
        "EXCEPT SELECT trade_date FROM institutional_trading GROUP BY trade_date"
    ).fetchall()]
    repaired = 0
    for value in gaps:
        trade_day = date.fromisoformat(value)
        frames = [fetch_institutional_twse(trade_day),
                  fetch_institutional_tpex_by_date(trade_day)]
        frames = [df[df["stock_id"].apply(_is_stock_code)]
                  for df in frames if not df.empty]
        if not frames:
            continue
        df = pd.concat(frames, ignore_index=True)
        repaired += upsert_df(
            conn,
            "institutional_trading",
            df[["stock_id", "trade_date", "total_net", "foreign_net", "invest_net"]],
            date_cols=("trade_date",),
        )
    conn.close()
    return {"gap_dates": len(gaps), "rows_written": repaired}


if __name__ == "__main__":
    print(repair_institutional_gaps())
