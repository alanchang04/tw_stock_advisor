"""
追蹤清單買點判斷測試（data_pipeline/analysis/watchlist_advisor.py）。

2026-07-20修正真bug：原本計分公式疊加的5個STRATEGY權重(w_ma_cross/w_breakout/
w_macd_pos/w_inst_buy/w_rsi_sweet)全部是0（2026-07-15策略重構後留下的死重量，
沒人回頭改這裡），扣掉唯一還有效的投信連3買+1.5分，永遠不夠格觸發🟡(2.0)或
🟢(4.0)門檻——這個功能上線後事實上不可能發出任何買點訊號。改用
agent.stock_analysis.get_full_scored_universe()+rank_in_universe()，跟每日AI選股
共用同一套完整評分邏輯，不再手動維護一份會過期的簡化版權重。

用真實DB（交易內回滾，不寫入正式資料，同 test_stock_analysis.py 手法），但把
get_full_scored_universe/rank_in_universe monkeypatch 掉，不需要真的跑全市場
候選篩選（那需要大量真實歷史資料才能有意義，這裡只驗證 watchlist_advisor 自己
的邏輯：SQL組裝、訊號分級、寫回是否正確）。
"""
import os
import sys
from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_pipeline.analysis.watchlist_advisor as wa


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

    monkeypatch.setattr(wa, "get_session", _shared)
    yield session
    session.rollback()
    session.close()


def _seed_watchlist_stock(tx, sid: str, close: float, rsi: float = 55.0,
                          target_price: float | None = None, username="testuser",
                          ma20: float | None = None):
    """建最小可用的 user → watchlist → watchlist_item → stock → 價量/指標 資料鏈。"""
    tx.execute(text("""
        INSERT INTO users (username, password_hash, role) VALUES (:u, 'x', 'user')
        ON CONFLICT (username) DO NOTHING
    """), {"u": username})
    uid = tx.execute(text("SELECT user_id FROM users WHERE username=:u"), {"u": username}).scalar()

    tx.execute(text("""
        INSERT INTO watchlists (user_id, list_name) VALUES (:uid, 'test_list')
        ON CONFLICT (user_id, list_name) DO NOTHING
    """), {"uid": uid})
    list_id = tx.execute(text(
        "SELECT list_id FROM watchlists WHERE user_id=:uid AND list_name='test_list'"),
        {"uid": uid}).scalar()

    tx.execute(text("INSERT INTO stocks (stock_id, stock_name, market) VALUES (:s, 'test', 'TWSE') "
                    "ON CONFLICT DO NOTHING"), {"s": sid})
    today = date.today()
    tx.execute(text("""
        INSERT INTO daily_prices (stock_id, trade_date, open, high, low, close, volume, turnover, change_pct)
        VALUES (:s, :d, :c, :c, :c, :c, 1000, :c*1000, 0)
        ON CONFLICT (stock_id, trade_date) DO UPDATE SET close = EXCLUDED.close
    """), {"s": sid, "d": today, "c": close})
    tx.execute(text("""
        INSERT INTO technical_indicators (stock_id, trade_date, rsi14, ma20)
        VALUES (:s, :d, :r, :ma20)
        ON CONFLICT (stock_id, trade_date) DO UPDATE SET rsi14 = EXCLUDED.rsi14, ma20 = EXCLUDED.ma20
    """), {"s": sid, "d": today, "r": rsi, "ma20": ma20})

    tx.execute(text("""
        INSERT INTO watchlist_items (list_id, stock_id, target_price)
        VALUES (:lid, :s, :tp)
        ON CONFLICT (list_id, stock_id) DO UPDATE SET target_price = EXCLUDED.target_price
    """), {"lid": list_id, "s": sid, "tp": target_price})
    return list_id


def _fake_universe():
    import pandas as pd
    df = pd.DataFrame({"stock_id": [], "score": []})
    df.attrs["hard_excluded"] = []
    return df


def test_evaluate_watchlist_empty_returns_none(tx, monkeypatch):
    monkeypatch.setattr(wa, "get_full_scored_universe", lambda cfg: _fake_universe())
    tx.execute(text("DELETE FROM watchlist_items"))
    assert wa.evaluate_watchlist(date.today()) is None


def test_green_signal_when_would_make_top_n(tx, monkeypatch):
    sid = "TESTWL01"
    _seed_watchlist_stock(tx, sid, close=100.0)
    monkeypatch.setattr(wa, "get_full_scored_universe", lambda cfg: _fake_universe())
    monkeypatch.setattr(wa, "rank_in_universe", lambda s, u, cfg: {
        "ok": True, "in_universe": True, "veto_reason": None,
        "score": 9.0, "rank": 2, "total_candidates": 100,
        "percentile_rank": 0.99, "would_make_top_n": True})

    result = wa.evaluate_watchlist(date.today())
    assert result is not None and sid in result and "🟢" in result

    sig = tx.execute(text("SELECT last_signal FROM watchlist_items WHERE stock_id=:s"),
                     {"s": sid}).scalar()
    assert "🟢" in sig and "買點浮現" in sig


def test_yellow_signal_when_near_but_not_top_n(tx, monkeypatch):
    sid = "TESTWL02"
    _seed_watchlist_stock(tx, sid, close=50.0)
    monkeypatch.setattr(wa, "get_full_scored_universe", lambda cfg: _fake_universe())
    monkeypatch.setattr(wa, "rank_in_universe", lambda s, u, cfg: {
        "ok": True, "in_universe": True, "veto_reason": None,
        "score": 5.0, "rank": 30, "total_candidates": 100,
        "percentile_rank": 0.75, "would_make_top_n": False})

    wa.evaluate_watchlist(date.today())
    sig = tx.execute(text("SELECT last_signal FROM watchlist_items WHERE stock_id=:s"),
                     {"s": sid}).scalar()
    assert "🟡" in sig and "接近買點" in sig


def test_red_signal_when_hard_vetoed(tx, monkeypatch):
    sid = "TESTWL03"
    _seed_watchlist_stock(tx, sid, close=50.0)
    monkeypatch.setattr(wa, "get_full_scored_universe", lambda cfg: _fake_universe())
    monkeypatch.setattr(wa, "rank_in_universe", lambda s, u, cfg: {
        "ok": True, "in_universe": False, "veto_reason": "乖離月線15%以上；",
        "score": None, "rank": None, "total_candidates": 100,
        "percentile_rank": None, "would_make_top_n": False})

    wa.evaluate_watchlist(date.today())
    sig = tx.execute(text("SELECT last_signal FROM watchlist_items WHERE stock_id=:s"),
                     {"s": sid}).scalar()
    assert "🔴" in sig and "乖離月線" in sig


def test_white_signal_when_low_percentile(tx, monkeypatch):
    sid = "TESTWL04"
    _seed_watchlist_stock(tx, sid, close=50.0, rsi=42.0)
    monkeypatch.setattr(wa, "get_full_scored_universe", lambda cfg: _fake_universe())
    monkeypatch.setattr(wa, "rank_in_universe", lambda s, u, cfg: {
        "ok": True, "in_universe": True, "veto_reason": None,
        "score": 1.0, "rank": 90, "total_candidates": 100,
        "percentile_rank": 0.10, "would_make_top_n": False})

    wa.evaluate_watchlist(date.today())
    sig = tx.execute(text("SELECT last_signal FROM watchlist_items WHERE stock_id=:s"),
                     {"s": sid}).scalar()
    assert "⚪" in sig and "觀望" in sig and "RSI 42" in sig


def test_target_price_annotation_when_reached(tx, monkeypatch):
    sid = "TESTWL05"
    _seed_watchlist_stock(tx, sid, close=40.0, target_price=45.0)   # 現價已低於目標價
    monkeypatch.setattr(wa, "get_full_scored_universe", lambda cfg: _fake_universe())
    monkeypatch.setattr(wa, "rank_in_universe", lambda s, u, cfg: {
        "ok": True, "in_universe": True, "veto_reason": None,
        "score": 1.0, "rank": 90, "total_candidates": 100,
        "percentile_rank": 0.10, "would_make_top_n": False})

    wa.evaluate_watchlist(date.today())
    sig = tx.execute(text("SELECT last_signal FROM watchlist_items WHERE stock_id=:s"),
                     {"s": sid}).scalar()
    assert "已到目標價" in sig


def test_falling_knife_watch_is_flagged_not_plain_neutral(tx, monkeypatch):
    # 國巨型態：跌破月線10%以上，不在候選池(in_universe=False,veto_reason=None)——
    # 舊版會落到跟盤整股一樣的中性⚪觀望，藏住接刀風險。現在應明講趨勢偏弱、勿接刀。
    sid = "TESTWL06"
    _seed_watchlist_stock(tx, sid, close=670.0, rsi=35.0, ma20=927.0)  # 月線下-27.7%
    monkeypatch.setattr(wa, "get_full_scored_universe", lambda cfg: _fake_universe())
    monkeypatch.setattr(wa, "rank_in_universe", lambda s, u, cfg: {
        "ok": True, "in_universe": False, "veto_reason": None,
        "score": None, "rank": None, "total_candidates": 100,
        "percentile_rank": None, "would_make_top_n": False})

    wa.evaluate_watchlist(date.today())
    sig = tx.execute(text("SELECT last_signal FROM watchlist_items WHERE stock_id=:s"),
                     {"s": sid}).scalar()
    assert "趨勢偏弱" in sig and "勿接刀" in sig
    assert "跌破月線 -27%" in sig or "跌破月線 -28%" in sig


def test_target_price_reached_but_falling_knife_still_warns(tx, monkeypatch):
    # 「已到目標價」但其實是接刀——不能只報喜訊，要同時警示趨勢偏弱
    sid = "TESTWL07"
    _seed_watchlist_stock(tx, sid, close=670.0, rsi=35.0, ma20=927.0, target_price=700.0)
    monkeypatch.setattr(wa, "get_full_scored_universe", lambda cfg: _fake_universe())
    monkeypatch.setattr(wa, "rank_in_universe", lambda s, u, cfg: {
        "ok": True, "in_universe": False, "veto_reason": None,
        "score": None, "rank": None, "total_candidates": 100,
        "percentile_rank": None, "would_make_top_n": False})

    wa.evaluate_watchlist(date.today())
    sig = tx.execute(text("SELECT last_signal FROM watchlist_items WHERE stock_id=:s"),
                     {"s": sid}).scalar()
    assert "趨勢偏弱" in sig and "已到目標價" in sig
