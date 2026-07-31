"""Audit the local historical dataset before accepting a backtest."""
from __future__ import annotations

import json
import os
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_pipeline.local_research_db import get_local_conn, ensure_local_tables


def audit(start: str = "2015-01-01", end: str | None = None) -> dict:
    end = end or str(date.today())
    conn = get_local_conn(); ensure_local_tables(conn)
    out = {"start": start, "end": end, "tables": {}, "progress": {}}
    for table, date_col in (("daily_prices", "trade_date"),
                            ("institutional_trading", "trade_date"),
                            ("dividend_events", "ex_date"),
                            ("monthly_revenue", "year_month"),
                            ("margin_trading", "trade_date")):
        row = conn.execute(
            f"SELECT COUNT(*), MIN({date_col}), MAX({date_col}), COUNT(DISTINCT {date_col}), "
            f"COUNT(DISTINCT stock_id) FROM {table}"
        ).fetchone()
        out["tables"][table] = dict(zip(
            ["rows", "first", "last", "periods", "stocks"], row))
    out["progress"] = dict(conn.execute(
        "SELECT task,last_date FROM backfill_progress ORDER BY task").fetchall())
    expected = len(pd.bdate_range(start, end))
    observed = out["tables"]["daily_prices"]["periods"]
    out["price_date_coverage"] = observed / expected if expected else 0
    out["expected_business_days"] = expected
    counts = pd.read_sql_query(
        "SELECT trade_date, COUNT(*) rows FROM daily_prices GROUP BY trade_date", conn)
    median_rows = float(counts["rows"].median()) if not counts.empty else 0
    out["median_stocks_per_day"] = median_rows
    out["suspicious_partial_days"] = (
        counts[counts["rows"] < median_rows * .5].to_dict("records") if median_rows else []
    )
    out["institutional_dates_missing_prices"] = [r[0] for r in conn.execute(
        "SELECT trade_date FROM institutional_trading GROUP BY trade_date "
        "EXCEPT SELECT trade_date FROM daily_prices GROUP BY trade_date"
    ).fetchall()]
    out["price_dates_missing_institutional"] = [r[0] for r in conn.execute(
        "SELECT trade_date FROM daily_prices GROUP BY trade_date "
        "EXCEPT SELECT trade_date FROM institutional_trading GROUP BY trade_date"
    ).fetchall()]
    conn.close()
    return out


if __name__ == "__main__":
    print(json.dumps(audit(), ensure_ascii=False, indent=2, default=str))
