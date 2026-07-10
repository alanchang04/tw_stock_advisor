"""
掛單→隔日開盤成交（階段0b）的整合測試。

安全性：get_session 被換成「共用同一個 session、永不 commit」的版本，
測試結束一律 rollback，因此對真實 schema 驗證但**不會**寫入任何資料。
無法連線 DB 時整檔 skip。

跑法：py -m pytest tests/test_portfolio_orders.py -v
"""
import os
import sys
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.portfolio as pf
from agent.strategy import SLIPPAGE, net_return, buy_fill, sell_fill


@pytest.fixture
def tx(monkeypatch):
    """把 portfolio 的 get_session 換成共用、不 commit 的 session，結束後 rollback。"""
    try:
        from database.connection import get_session_factory
        session = get_session_factory()()
        session.execute(text("SELECT 1"))
    except Exception as e:                       # 無 DB / 無網路
        pytest.skip(f"DB 無法連線，跳過整合測試：{e}")

    @contextmanager
    def _shared():
        yield session                            # 關鍵：不 commit

    monkeypatch.setattr(pf, "get_session", _shared)
    pf.ensure_positions_table()
    pf.ensure_pending_orders_table()
    # 清空 AI 持倉以釋出名額（僅在此交易內，最後會 rollback）
    session.execute(text(
        "UPDATE positions SET status='closed' WHERE status='open' AND COALESCE(source,'ai')='ai'"))
    session.execute(text("DELETE FROM pending_orders WHERE status='pending'"))
    yield session
    session.rollback()
    session.close()


def _a_stock_with_open(session):
    """找一檔最新交易日有開盤價的股票，回傳 (stock_id, trade_date, open)。"""
    r = session.execute(text("""
        SELECT stock_id, trade_date, open FROM daily_prices
        WHERE open > 0 AND trade_date = (SELECT MAX(trade_date) FROM daily_prices)
        ORDER BY stock_id LIMIT 1
    """)).fetchone()
    return r[0], r[1], float(r[2])


# ── 純函式（不需 DB）──────────────────────────────────────────────
def test_fill_prices_apply_slippage_in_the_costly_direction():
    assert buy_fill(100.0) == pytest.approx(100.0 * (1 + SLIPPAGE))   # 買貴一點
    assert sell_fill(100.0) == pytest.approx(100.0 * (1 - SLIPPAGE))  # 賣便宜一點
    assert buy_fill(100.0) > sell_fill(100.0)


def test_net_return_is_below_gross_return():
    gross = 110 / 100 - 1
    assert net_return(100, 110) < gross          # 手續費+證交稅一定讓淨報酬更低


# ── 整合（真實 schema，交易內回滾）────────────────────────────────
def test_buy_order_fills_at_next_day_open_plus_slippage(tx):
    sid, d, op = _a_stock_with_open(tx)
    tx.execute(text("""
        INSERT INTO pending_orders (side, stock_id, signal_date, signal_price, reason)
        VALUES ('buy', :sid, :sd, 99.0, 'test')
    """), {"sid": sid, "sd": d - timedelta(days=1)})

    filled = pf.fill_pending_orders(d)

    assert len(filled["entries"]) == 1
    assert filled["entries"][0]["stock_id"] == sid
    assert filled["entries"][0]["entry_price"] == pytest.approx(buy_fill(op), rel=1e-6)

    pos = tx.execute(text("""
        SELECT entry_price, signal_price FROM positions
        WHERE stock_id=:sid AND entry_date=:d AND status='open'
    """), {"sid": sid, "d": d}).fetchone()
    assert pos is not None
    assert float(pos[0]) == pytest.approx(round(buy_fill(op), 2))
    assert float(pos[1]) == pytest.approx(99.0)   # 訊號價保留，供日後校準滑價


def test_sell_order_fills_at_next_day_open_minus_slippage_and_records_net(tx):
    sid, d, op = _a_stock_with_open(tx)
    entry_px = op * 0.9        # 假設當初便宜買進
    pid = tx.execute(text("""
        INSERT INTO positions (stock_id, entry_date, entry_price, peak_price, source)
        VALUES (:sid, :ed, :px, :px, 'ai') RETURNING id
    """), {"sid": sid, "ed": d - timedelta(days=5), "px": entry_px}).scalar()
    tx.execute(text("""
        INSERT INTO pending_orders (side, stock_id, signal_date, signal_price, reason, position_id)
        VALUES ('sell', :sid, :sd, 123.0, '死亡交叉', :pid)
    """), {"sid": sid, "sd": d - timedelta(days=1), "pid": pid})

    filled = pf.fill_pending_orders(d)

    assert len(filled["exits"]) == 1
    e = filled["exits"][0]
    assert e["exit_price"] == pytest.approx(sell_fill(op), rel=1e-6)
    # 淨報酬必須低於毛報酬（扣了手續費+證交稅）
    assert e["net_return_pct"] < e["return_pct"]

    row = tx.execute(text(
        "SELECT status, return_pct, net_return_pct, exit_signal_price FROM positions WHERE id=:i"
    ), {"i": pid}).fetchone()
    assert row[0] == "closed"
    assert float(row[2]) < float(row[1])          # net < gross
    assert float(row[3]) == pytest.approx(123.0)  # 出場訊號價有存


def test_same_day_signal_is_not_filled_today(tx):
    """今晚剛掛的單不能今晚就成交（否則又變回買不到的當日價）。"""
    sid, d, _ = _a_stock_with_open(tx)
    tx.execute(text("""
        INSERT INTO pending_orders (side, stock_id, signal_date, signal_price, reason)
        VALUES ('buy', :sid, :sd, 99.0, 'test')
    """), {"sid": sid, "sd": d})                  # signal_date == on_date

    filled = pf.fill_pending_orders(d)
    assert filled["entries"] == []
    still = tx.execute(text(
        "SELECT status FROM pending_orders WHERE stock_id=:sid AND side='buy'"
    ), {"sid": sid}).scalar()
    assert still == "pending"


def test_stale_buy_order_expires_instead_of_chasing(tx):
    sid, d, _ = _a_stock_with_open(tx)
    tx.execute(text("""
        INSERT INTO pending_orders (side, stock_id, signal_date, signal_price, reason)
        VALUES ('buy', :sid, :sd, 99.0, 'test')
    """), {"sid": sid, "sd": d - timedelta(days=pf.BUY_ORDER_STALE_DAYS + 1)})

    filled = pf.fill_pending_orders(d)
    assert filled["entries"] == []
    st = tx.execute(text(
        "SELECT status FROM pending_orders WHERE stock_id=:sid AND side='buy'"
    ), {"sid": sid}).scalar()
    assert st == "expired"


def test_open_positions_never_includes_manual_positions(tx):
    """已知地雷：手動倉若被當成 AI 倉，會被自動掛單平掉。"""
    sid, d, op = _a_stock_with_open(tx)
    tx.execute(text("""
        INSERT INTO positions (stock_id, entry_date, entry_price, peak_price, source)
        VALUES (:sid, :ed, :px, :px, 'manual')
    """), {"sid": sid, "ed": d - timedelta(days=3), "px": op})

    assert all(p["stock_id"] != sid for p in pf.open_positions())


def test_queue_entries_counts_pending_buys_against_position_cap(tx, monkeypatch):
    """未成交的買單也要佔用持倉名額，否則會超買。"""
    sid, d, _ = _a_stock_with_open(tx)
    monkeypatch.setitem(pf.STRATEGY, "max_open_positions", 1)
    tx.execute(text("""
        INSERT INTO pending_orders (side, stock_id, signal_date, signal_price, reason)
        VALUES ('buy', :sid, :sd, 99.0, 'test')
    """), {"sid": sid, "sd": d})

    other = tx.execute(text("""
        SELECT stock_id FROM daily_prices
        WHERE trade_date = :d AND open > 0 AND stock_id <> :sid ORDER BY stock_id LIMIT 1
    """), {"d": d, "sid": sid}).scalar()

    queued = pf.queue_entries([{"stock_id": other, "reason": "test"}], d)
    assert queued == []          # 名額已被未成交買單佔滿
