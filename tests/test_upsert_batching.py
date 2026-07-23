"""
批次寫入正確性測試（data_pipeline/fetchers/finmind_fetcher.py，2026-07-23）。

背景：`upsert_daily_prices` / `upsert_institutional` 原本是
`for _, row in df.iterrows(): session.execute(INSERT ...)`，等於「一列一次 DB 往返」。
Neon 在雲端，實測往返 62ms（本機）；全市場約 1,950 檔 → 光價格表就 ~121 秒，
加上法人表同樣寫法 ≈ 240 秒以上，**這是 data_ingest 平均 528 秒（佔全流程 68%）的主因**。
決策軌跡的實測佐證：資料已是最新、backfill 空跑那兩次 data_ingest 只要 61~63 秒，
其餘 650~900 秒都花在這裡。

改成 executemany 批次（同 technical.py 既有作法）後實測：1,948 列從 121 秒 → 0.4 秒。

這裡測的是**正確性**（批次化不能改變資料語意）：插入、ON CONFLICT 更新、
空 DataFrame、NaN→NULL。用真實 DB 交易內回滾。
"""
import os
import sys
from contextlib import contextmanager
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_pipeline.fetchers.finmind_fetcher as ff

SID = "TESTUPS1"


@pytest.fixture
def tx(monkeypatch):
    try:
        from database.connection import get_session_factory
        session = get_session_factory()()
        session.execute(text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"DB 無法連線，跳過整合測試：{e}")

    @contextmanager
    def _shared():
        yield session

    monkeypatch.setattr(ff, "get_session", _shared)
    session.execute(text("INSERT INTO stocks (stock_id, stock_name, market) "
                         "VALUES (:s, 'test', 'TWSE') ON CONFLICT DO NOTHING"), {"s": SID})
    yield session
    session.rollback()
    session.close()


def _px(close, volume=1000, d=date(2026, 7, 22)):
    return pd.DataFrame([{
        "stock_id": SID, "trade_date": d, "open": close, "high": close,
        "low": close, "close": close, "volume": volume,
        "turnover": close * volume, "change_pct": 1.5,
    }])


def test_batch_insert_writes_rows(tx):
    ff.upsert_daily_prices(_px(100.0))
    row = tx.execute(text("SELECT close, volume FROM daily_prices "
                          "WHERE stock_id=:s AND trade_date='2026-07-22'"), {"s": SID}).fetchone()
    assert row is not None
    assert float(row[0]) == 100.0 and int(row[1]) == 1000


def test_batch_upsert_updates_on_conflict(tx):
    ff.upsert_daily_prices(_px(100.0))
    ff.upsert_daily_prices(_px(123.5, volume=2000))     # 同一 (stock_id, trade_date)
    rows = tx.execute(text("SELECT close, volume FROM daily_prices "
                           "WHERE stock_id=:s AND trade_date='2026-07-22'"), {"s": SID}).fetchall()
    assert len(rows) == 1                                # 沒有重複列
    assert float(rows[0][0]) == 123.5 and int(rows[0][1]) == 2000


def test_batch_insert_multiple_rows_in_one_call(tx):
    df = pd.concat([_px(10.0, d=date(2026, 7, 20)),
                    _px(11.0, d=date(2026, 7, 21)),
                    _px(12.0, d=date(2026, 7, 22))], ignore_index=True)
    ff.upsert_daily_prices(df)
    n = tx.execute(text("SELECT COUNT(*) FROM daily_prices WHERE stock_id=:s"), {"s": SID}).scalar()
    assert n == 3


def test_nan_becomes_null_not_error(tx):
    df = _px(100.0)
    df.loc[0, "change_pct"] = float("nan")
    ff.upsert_daily_prices(df)                            # 不可拋例外
    v = tx.execute(text("SELECT change_pct FROM daily_prices "
                        "WHERE stock_id=:s AND trade_date='2026-07-22'"), {"s": SID}).scalar()
    assert v is None


def test_empty_dataframe_is_noop(tx):
    ff.upsert_daily_prices(pd.DataFrame())                # 不可拋例外
    ff.upsert_institutional(pd.DataFrame())


def test_institutional_batch_upsert(tx):
    def _inst(total):
        return pd.DataFrame([{
            "stock_id": SID, "trade_date": date(2026, 7, 22),
            "foreign_buy": 1, "foreign_sell": 2, "foreign_net": -1,
            "invest_buy": 3, "invest_sell": 1, "invest_net": 2,
            "dealer_buy": 0, "dealer_sell": 0, "dealer_net": 0, "total_net": total,
        }])
    ff.upsert_institutional(_inst(100))
    ff.upsert_institutional(_inst(250))                   # 更新
    rows = tx.execute(text("SELECT total_net, invest_net FROM institutional_trading "
                           "WHERE stock_id=:s AND trade_date='2026-07-22'"), {"s": SID}).fetchall()
    assert len(rows) == 1
    assert int(rows[0][0]) == 250 and int(rows[0][1]) == 2
