"""
scripts/db_query.py

用專案現有的連線設定（.env 的 DATABASE_URL）直接對資料庫下 SQL，不必另外裝 psql。

用法：
    python scripts/db_query.py "SELECT COUNT(*) FROM daily_prices"
    python scripts/db_query.py --tables                # 列出所有資料表 + 列數
    python scripts/db_query.py --schema daily_prices   # 看某張表的欄位
    python scripts/db_query.py --size                  # 看資料庫/各表容量

注意：預設只允許 SELECT/WITH 等唯讀查詢，避免手滑改到正式資料。
      要跑寫入指令請加 --write（自行確認你在做什麼）。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from sqlalchemy import text
from database.connection import get_session

READONLY_PREFIXES = ("select", "with", "explain", "show", "table")


def _run(sql: str, write: bool = False):
    head = sql.strip().split(None, 1)[0].lower() if sql.strip() else ""
    if not write and head not in READONLY_PREFIXES:
        sys.exit(f"拒絕執行非唯讀指令 '{head}'。確定要寫入請加 --write。")
    with get_session() as s:
        try:
            df = pd.read_sql(text(sql), s.bind)
        except Exception:          # 沒有回傳列的指令（write 模式）
            s.execute(text(sql))
            print("執行完成（無回傳結果）")
            return
    if df.empty:
        print("(查無資料)")
    else:
        with pd.option_context("display.max_rows", 100, "display.width", 200):
            print(df.to_string(index=False))
        print(f"\n({len(df)} 列)")


def _tables():
    _run("""
        SELECT relname AS 資料表, n_live_tup AS 估計列數
        FROM pg_stat_user_tables ORDER BY n_live_tup DESC
    """)


def _schema(table: str):
    _run(f"""
        SELECT column_name AS 欄位, data_type AS 型別, is_nullable AS 可為空
        FROM information_schema.columns
        WHERE table_name = '{table}' ORDER BY ordinal_position
    """)


def _size():
    _run("""
        SELECT relname AS 資料表,
               pg_size_pretty(pg_total_relation_size(relid)) AS 容量
        FROM pg_catalog.pg_statio_user_tables
        ORDER BY pg_total_relation_size(relid) DESC
    """)
    _run("SELECT pg_size_pretty(pg_database_size(current_database())) AS 資料庫總容量")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("sql", nargs="?", help="要執行的 SQL")
    ap.add_argument("--tables", action="store_true", help="列出所有資料表與列數")
    ap.add_argument("--schema", metavar="TABLE", help="看某張表的欄位")
    ap.add_argument("--size", action="store_true", help="看資料庫與各表容量")
    ap.add_argument("--write", action="store_true", help="允許非唯讀指令")
    a = ap.parse_args()

    if a.tables:
        _tables()
    elif a.schema:
        _schema(a.schema)
    elif a.size:
        _size()
    elif a.sql:
        _run(a.sql, write=a.write)
    else:
        ap.print_help()
