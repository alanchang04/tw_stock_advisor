"""
data_pipeline/local_research_db.py

本機 SQLite 研究資料庫（SPEC_QUANT_UPGRADE.md P0，2026-07-17 架構調整）。

**為什麼不直接寫 Neon**：10年歷史回補（價量+法人逐日回補+多次 parquet 匯出）
在單次測試中就把 Neon 免費專案的資料傳輸配額燒穿，連正式每日 pipeline 都連不上
（真實事故：2026-07-17 22:15~23:05，回補跑50分鐘只到2015-01-21就被伺服器強制斷線，
之後除權息回補/parquet匯出全部連鎖失敗，回報「Your project has exceeded the data
transfer quota」）。10年規模的研究資料庫本來就不該養在按流量計費的雲端小免費層，
改成本機 SQLite：完全不碰 Neon，回補多久、重跑幾次都不會燒配額；回補完只把
「最終研究結果」（parquet）留在本機供 `run_backtest(parquet_dir=...)` 讀，
Neon 繼續只服務每日 pipeline 需要的近期資料，兩邊資料流完全分開。

用法：
    from data_pipeline.local_research_db import get_local_conn, ensure_local_tables
    conn = get_local_conn()
    ensure_local_tables(conn)
"""
from __future__ import annotations
import os
import sqlite3
from datetime import date

import pandas as pd

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "research", "research.db"
)


def get_local_conn(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")   # 允許回補中途被中斷也不損毀既有資料
    return conn


def ensure_local_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS daily_prices (
            stock_id TEXT NOT NULL, trade_date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, turnover REAL, change_pct REAL,
            PRIMARY KEY (stock_id, trade_date)
        );
        CREATE TABLE IF NOT EXISTS institutional_trading (
            stock_id TEXT NOT NULL, trade_date TEXT NOT NULL,
            total_net REAL, foreign_net REAL, invest_net REAL,
            PRIMARY KEY (stock_id, trade_date)
        );
        CREATE TABLE IF NOT EXISTS dividend_events (
            stock_id TEXT NOT NULL, ex_date TEXT NOT NULL,
            pre_close REAL, ref_price REAL,
            PRIMARY KEY (stock_id, ex_date)
        );
        CREATE TABLE IF NOT EXISTS delisted_stocks (
            stock_id TEXT PRIMARY KEY, stock_name TEXT,
            delisting_date TEXT, market TEXT
        );
        CREATE TABLE IF NOT EXISTS monthly_revenue (
            stock_id TEXT NOT NULL, year_month TEXT NOT NULL,
            revenue REAL, mom_pct REAL, yoy_pct REAL,
            PRIMARY KEY (stock_id, year_month)
        );
        CREATE TABLE IF NOT EXISTS backfill_progress (
            task TEXT PRIMARY KEY, last_date TEXT
        );
    """)
    conn.commit()


# 各表的 PRIMARY KEY 欄位（ON CONFLICT 需要明確指定衝突目標，SQLite 不會自動推斷）
_PK_COLS = {
    "daily_prices": ("stock_id", "trade_date"),
    "institutional_trading": ("stock_id", "trade_date"),
    "dividend_events": ("stock_id", "ex_date"),
    "delisted_stocks": ("stock_id",),
    "monthly_revenue": ("stock_id", "year_month"),
}


def upsert_df(conn: sqlite3.Connection, table: str, df: pd.DataFrame, date_cols: tuple[str, ...] = ()):
    """通用 upsert：df 欄位需與 table 完全對應，PK 由 _PK_COLS 決定（重跑/續跑安全，不重複）。"""
    if df.empty:
        return 0
    df = df.copy()
    for c in date_cols:
        if c in df.columns:
            df[c] = df[c].astype(str)
    cols = list(df.columns)
    pk = _PK_COLS[table]
    placeholders = ",".join("?" for _ in cols)
    col_list = ",".join(cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in pk)
    conflict_target = ",".join(pk)
    sql = (f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
          f"ON CONFLICT ({conflict_target}) DO UPDATE SET {updates}") if updates else \
          f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"
    conn.executemany(sql, df[cols].itertuples(index=False, name=None))
    conn.commit()
    return len(df)


def get_progress(conn: sqlite3.Connection, task: str) -> date | None:
    row = conn.execute("SELECT last_date FROM backfill_progress WHERE task=?", (task,)).fetchone()
    if not row or not row[0]:
        return None
    y, m, d = map(int, row[0].split("-"))
    return date(y, m, d)


def set_progress(conn: sqlite3.Connection, task: str, d: date):
    conn.execute(
        "INSERT INTO backfill_progress (task, last_date) VALUES (?, ?) "
        "ON CONFLICT(task) DO UPDATE SET last_date=excluded.last_date",
        (task, d.isoformat()),
    )
    conn.commit()


def export_to_parquet(conn: sqlite3.Connection, out_dir: str):
    """把本機 SQLite 內容匯出成 run_backtest(parquet_dir=...) 讀的 parquet 檔。"""
    import os as _os
    _os.makedirs(out_dir, exist_ok=True)

    prices = pd.read_sql_query(
        "SELECT stock_id, trade_date, open, high, low, close, volume, turnover, change_pct FROM daily_prices",
        conn)
    prices.to_parquet(_os.path.join(out_dir, "prices.parquet"), index=False)

    inst = pd.read_sql_query(
        "SELECT stock_id, trade_date, total_net, foreign_net, invest_net FROM institutional_trading", conn)
    inst.to_parquet(_os.path.join(out_dir, "institutional.parquet"), index=False)

    div = pd.read_sql_query("SELECT stock_id, ex_date, pre_close, ref_price FROM dividend_events", conn)
    div.to_parquet(_os.path.join(out_dir, "dividend_events.parquet"), index=False)

    rev = pd.read_sql_query(
        "SELECT stock_id, year_month, revenue, mom_pct, yoy_pct FROM monthly_revenue", conn)
    rev.to_parquet(_os.path.join(out_dir, "monthly_revenue.parquet"), index=False)

    return {"prices": len(prices), "institutional": len(inst), "dividend_events": len(div),
            "monthly_revenue": len(rev)}
