"""
本機研究資料庫測試（data_pipeline/local_research_db.py，SPEC_QUANT_UPGRADE.md P0
架構調整：2026-07-17 Neon資料傳輸配額用盡事故後，10年歷史回補改寫本機SQLite）。
全部用 in-memory SQLite（sqlite3.connect(":memory:")），不碰真實檔案/網路。
"""
import os
import sys
import sqlite3
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

from data_pipeline.local_research_db import (
    ensure_local_tables, upsert_df, get_progress, set_progress, export_to_parquet,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    ensure_local_tables(c)
    yield c
    c.close()


def test_ensure_local_tables_idempotent(conn):
    ensure_local_tables(conn)   # 第二次呼叫不應報錯
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"daily_prices", "institutional_trading", "dividend_events",
           "delisted_stocks", "monthly_revenue", "backfill_progress"} <= tables


def test_upsert_df_inserts_new_rows(conn):
    df = pd.DataFrame({
        "stock_id": ["2330", "2454"], "trade_date": [date(2024, 1, 2), date(2024, 1, 2)],
        "open": [500.0, 800.0], "high": [510.0, 810.0], "low": [495.0, 795.0],
        "close": [505.0, 805.0], "volume": [1000.0, 2000.0], "turnover": [1e8, 2e8],
        "change_pct": [1.0, 0.5],
    })
    n = upsert_df(conn, "daily_prices", df, date_cols=("trade_date",))
    assert n == 2
    row = conn.execute("SELECT close FROM daily_prices WHERE stock_id='2330'").fetchone()
    assert row[0] == 505.0


def test_upsert_df_updates_on_conflict_not_duplicates(conn):
    df1 = pd.DataFrame({"stock_id": ["2330"], "trade_date": [date(2024, 1, 2)],
                        "open": [500.0], "high": [510.0], "low": [495.0], "close": [505.0],
                        "volume": [1000.0], "turnover": [1e8], "change_pct": [1.0]})
    df2 = df1.copy()
    df2["close"] = 999.0   # 同一天同一檔股票，模擬重跑/資料更新
    upsert_df(conn, "daily_prices", df1, date_cols=("trade_date",))
    upsert_df(conn, "daily_prices", df2, date_cols=("trade_date",))
    rows = conn.execute("SELECT close FROM daily_prices WHERE stock_id='2330'").fetchall()
    assert len(rows) == 1                 # 沒有重複列
    assert rows[0][0] == 999.0            # 值被更新成最新一次

def test_upsert_df_empty_returns_zero(conn):
    assert upsert_df(conn, "daily_prices", pd.DataFrame()) == 0


def test_upsert_df_dividend_events_pk_stock_and_exdate(conn):
    df = pd.DataFrame({"stock_id": ["1101"], "ex_date": [date(2024, 7, 1)],
                       "pre_close": [34.2], "ref_price": [33.2]})
    n1 = upsert_df(conn, "dividend_events", df, date_cols=("ex_date",))
    n2 = upsert_df(conn, "dividend_events", df, date_cols=("ex_date",))   # 重跑同一批
    assert n1 == 1 and n2 == 1
    rows = conn.execute("SELECT * FROM dividend_events").fetchall()
    assert len(rows) == 1                 # upsert不會重複累積


def test_get_set_progress_roundtrip(conn):
    assert get_progress(conn, "task_a") is None
    set_progress(conn, "task_a", date(2024, 3, 15))
    assert get_progress(conn, "task_a") == date(2024, 3, 15)
    set_progress(conn, "task_a", date(2024, 3, 16))   # 更新覆蓋，不是新增
    assert get_progress(conn, "task_a") == date(2024, 3, 16)


def test_get_progress_independent_per_task(conn):
    set_progress(conn, "task_a", date(2024, 1, 1))
    set_progress(conn, "task_b", date(2025, 6, 1))
    assert get_progress(conn, "task_a") == date(2024, 1, 1)
    assert get_progress(conn, "task_b") == date(2025, 6, 1)


def test_export_to_parquet_writes_expected_files(conn, tmp_path):
    df = pd.DataFrame({"stock_id": ["2330"], "trade_date": [date(2024, 1, 2)],
                       "open": [500.0], "high": [510.0], "low": [495.0], "close": [505.0],
                       "volume": [1000.0], "turnover": [1e8], "change_pct": [1.0]})
    upsert_df(conn, "daily_prices", df, date_cols=("trade_date",))
    stats = export_to_parquet(conn, str(tmp_path))
    assert stats["prices"] == 1
    assert os.path.exists(os.path.join(tmp_path, "prices.parquet"))
    assert os.path.exists(os.path.join(tmp_path, "institutional.parquet"))
    assert os.path.exists(os.path.join(tmp_path, "dividend_events.parquet"))
    out = pd.read_parquet(os.path.join(tmp_path, "prices.parquet"))
    assert out.iloc[0]["stock_id"] == "2330"
