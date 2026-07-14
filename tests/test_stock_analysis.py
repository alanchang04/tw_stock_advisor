"""
個股隨選分析測試（agent/stock_analysis.py）。
- 純函式（_build_synthesis_prompt）：不需 DB/LLM。
- DB 整合（_stock_basic/_institutional_flow/_recent_news）：monkeypatch get_session
  換成不 commit 的共用交易（同 test_portfolio_orders.py 手法），驗證後 rollback。
- analyze_stock() 對不存在股票的早退路徑：不需 LLM，驗證不會半途噴例外。
"""
import os
import sys
from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.stock_analysis as sa


# ── 純函式：裁決 prompt 組裝 ───────────────────────────────────────
def _basic():
    return dict(stock_name="台積電", industry="半導體業", trade_date=date(2026, 7, 13),
               close=1000.0, change_pct=1.5, rsi14=60.0, macd_hist=0.5,
               ma5=990, ma20=970, ma60=950, signal_ma_cross=0, signal_breakout=1)


def test_prompt_requires_citation_and_both_sides():
    sys_p, user_p = sa._build_synthesis_prompt(
        "2330", _basic(), {"rs20": 0.9, "stack_days": 10}, 15.0,
        {"bull": True, "ok": True, "close": 106.0, "ma60": 99.0},
        {"ok": True, "invest_streak_days": 5, "invest_streak_lots": 100.0,
         "foreign_streak_days": 0, "foreign_streak_lots": 0.0, "latest_date": date(2026, 7, 13)},
        [])
    assert "禁止空泛形容" in sys_p
    assert "bull_points" in sys_p and "bear_points" in sys_p
    assert "data_gaps" in sys_p
    assert "台積電" in user_p and "RS20" in user_p


def test_prompt_flags_missing_institutional_data():
    _, user_p = sa._build_synthesis_prompt(
        "2330", _basic(), {}, None,
        {"bull": True, "ok": True, "close": 106.0, "ma60": 99.0},
        {"ok": False}, [])
    assert "查無法人買賣超資料" in user_p
    assert "不可假設無風險" in user_p


def test_prompt_flags_missing_market_regime_data():
    _, user_p = sa._build_synthesis_prompt(
        "2330", _basic(), {}, None, {"bull": True, "ok": False}, {"ok": False}, [])
    assert "查無資料，保守視為多頭" in user_p


def test_prompt_includes_news_when_present():
    _, user_p = sa._build_synthesis_prompt(
        "2330", _basic(), {}, None, {"bull": True, "ok": False}, {"ok": False},
        [{"date": date(2026, 7, 10), "sentiment": "positive", "title": "台積電法說會樂觀"}])
    assert "台積電法說會樂觀" in user_p


def test_prompt_no_news_says_so():
    _, user_p = sa._build_synthesis_prompt(
        "2330", _basic(), {}, None, {"bull": True, "ok": False}, {"ok": False}, [])
    assert "近30日查無相關報導" in user_p


# ── analyze_stock 早退路徑：不存在的股票，不呼叫 LLM ────────────────
def test_analyze_stock_unknown_id_short_circuits(monkeypatch):
    monkeypatch.setattr(sa, "_stock_basic", lambda sid: None)
    called = {"llm": False}
    monkeypatch.setattr("agent.llm_advisor._ask", lambda *a, **k: called.__setitem__("llm", True))
    result = sa.analyze_stock("NOPE9999")
    assert result["ok"] is False
    assert "查無股票代號" in result["error"]
    assert called["llm"] is False


# ── DB 整合（真實 schema，交易內回滾，不寫入正式資料）──────────────
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

    monkeypatch.setattr(sa, "get_session", _shared)
    yield session
    session.rollback()
    session.close()


def test_stock_basic_reads_latest_row_for_known_stock(tx):
    sid = tx.execute(text("""
        SELECT p.stock_id FROM daily_prices p
        WHERE p.trade_date = (SELECT MAX(trade_date) FROM daily_prices) AND p.close > 0
        LIMIT 1
    """)).scalar()
    basic = sa._stock_basic(sid)
    assert basic is not None
    assert basic["close"] is not None
    assert basic["stock_name"]


def test_stock_basic_none_for_unknown_stock(tx):
    assert sa._stock_basic("NOPE9999") is None


def test_institutional_flow_lots_conversion_and_streak(tx):
    sid = "TESTINST01"
    tx.execute(text("INSERT INTO stocks (stock_id, stock_name, market) VALUES (:s, 'test', 'TWSE') ON CONFLICT DO NOTHING"),
              {"s": sid})
    tx.execute(text("DELETE FROM institutional_trading WHERE stock_id = :s"), {"s": sid})
    rows = [
        (date(2026, 7, 1), 1_000_000, 0, 1_000_000),   # 投信連買第1日：1000張
        (date(2026, 7, 2), 2_000_000, 0, 2_000_000),   # 第2日：2000張
        (date(2026, 7, 3), -500_000, 0, -500_000),     # 中斷：連買到此為止
    ]
    for d, inv, frn, tot in rows:
        tx.execute(text("""
            INSERT INTO institutional_trading (stock_id, trade_date, invest_net, foreign_net, total_net)
            VALUES (:s, :d, :inv, :frn, :tot)
        """), {"s": sid, "d": d, "inv": inv, "frn": frn, "tot": tot})

    result = sa._institutional_flow(sid, days=10)
    assert result["ok"] is True
    assert result["invest_streak_days"] == 0          # 最新一天(7/3)是負值，連買=0
    assert result["days"][-1]["invest_lots"] == -500.0  # 股→張換算正確(÷1000)


def test_institutional_flow_no_data_returns_not_ok(tx):
    assert sa._institutional_flow("NOPE9999")["ok"] is False


def test_recent_news_filters_by_related_stocks(tx):
    sid = "TESTNEWS01"
    tx.execute(text("INSERT INTO stocks (stock_id, stock_name, market) VALUES (:s, 'test', 'TWSE') ON CONFLICT DO NOTHING"),
              {"s": sid})
    tx.execute(text("""
        INSERT INTO market_signals (signal_type, source, title, related_stocks, signal_date, sentiment)
        VALUES ('news', 'unit-test', '單元測試新聞標題', ARRAY[:s], CURRENT_DATE, 'neutral')
    """), {"s": sid})
    news = sa._recent_news(sid)
    assert len(news) == 1 and news[0]["title"] == "單元測試新聞標題"


def test_recent_news_empty_for_unrelated_stock(tx):
    assert sa._recent_news("NOPE9999") == []
