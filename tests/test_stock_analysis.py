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

import pandas as pd
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


# ── AI選股評分套用到個股分析（2026-07-20新增：套用每日AI選股同一套邏輯）──────
def test_prompt_includes_ai_score_when_in_universe():
    ai_score = {"ok": True, "in_universe": True, "veto_reason": None,
               "score": 8.5, "rank": 3, "total_candidates": 120,
               "percentile_rank": 0.983, "would_make_top_n": True}
    _, user_p = sa._build_synthesis_prompt(
        "2330", _basic(), {}, None, {"bull": True, "ok": False}, {"ok": False}, [], ai_score)
    assert "第 3/120 名" in user_p
    assert "目前分數足以進入每日實際推薦名單" in user_p


def test_prompt_includes_ai_score_veto_reason():
    ai_score = {"ok": True, "in_universe": False, "veto_reason": "乖離月線15%以上；",
               "score": None, "rank": None, "total_candidates": 120,
               "percentile_rank": None, "would_make_top_n": False}
    _, user_p = sa._build_synthesis_prompt(
        "2330", _basic(), {}, None, {"bull": True, "ok": False}, {"ok": False}, [], ai_score)
    assert "被空方硬否決規則排除" in user_p
    assert "乖離月線15%以上" in user_p


def test_prompt_includes_ai_score_not_in_pool():
    ai_score = {"ok": True, "in_universe": False, "veto_reason": None,
               "score": None, "rank": None, "total_candidates": 120,
               "percentile_rank": None, "would_make_top_n": False}
    _, user_p = sa._build_synthesis_prompt(
        "2330", _basic(), {}, None, {"bull": True, "ok": False}, {"ok": False}, [], ai_score)
    assert "不在候選池內" in user_p


def test_prompt_without_ai_score_omits_section():
    # ai_score=None（預設值）：不加這段，舊呼叫方式（沒傳這個參數）不受影響
    _, user_p = sa._build_synthesis_prompt(
        "2330", _basic(), {}, None, {"bull": True, "ok": False}, {"ok": False}, [])
    assert "AI選股系統" not in user_p


def test_ai_selection_score_ranks_and_computes_percentile(monkeypatch):
    universe = pd.DataFrame({
        "stock_id": ["9999", "2330", "1101"],
        "score": [10.0, 8.0, 3.0],
    })
    universe.attrs["hard_excluded"] = []
    monkeypatch.setattr("agent.stock_selector.get_candidate_stocks", lambda *a, **k: universe)
    result = sa._ai_selection_score("2330", cfg={"pick_top_n": 5, "sector_exposure_cap": 0.6})
    assert result["ok"] is True
    assert result["in_universe"] is True
    assert result["rank"] == 2
    assert result["total_candidates"] == 3
    assert result["would_make_top_n"] is True   # rank 2 <= pick_top_n 5


def test_ai_selection_score_veto_reason_when_hard_excluded(monkeypatch):
    universe = pd.DataFrame({"stock_id": ["9999"], "score": [10.0]})
    universe.attrs["hard_excluded"] = [
        {"stock_id": "2330", "stock_name": "台積電", "hard_veto_reason": "乖離月線15%以上；"}]
    monkeypatch.setattr("agent.stock_selector.get_candidate_stocks", lambda *a, **k: universe)
    result = sa._ai_selection_score("2330", cfg={"pick_top_n": 5})
    assert result["ok"] is True
    assert result["in_universe"] is False
    assert "乖離月線" in result["veto_reason"]


def test_ai_selection_score_not_in_pool_when_missing(monkeypatch):
    universe = pd.DataFrame({"stock_id": ["9999"], "score": [10.0]})
    universe.attrs["hard_excluded"] = []
    monkeypatch.setattr("agent.stock_selector.get_candidate_stocks", lambda *a, **k: universe)
    result = sa._ai_selection_score("2330", cfg={"pick_top_n": 5})
    assert result["ok"] is True
    assert result["in_universe"] is False
    assert result["veto_reason"] is None


def test_ai_selection_score_disables_sector_cap_for_stable_ranking(monkeypatch):
    # sector_exposure_cap 應該被強制關閉（個股評分不該被暫時性的組合層限制影響）
    captured_cfg = {}

    def fake_get_candidates(industry_codes, top_n=99999, cfg=None):
        captured_cfg.update(cfg or {})
        df = pd.DataFrame({"stock_id": ["2330"], "score": [8.0]})
        df.attrs["hard_excluded"] = []
        return df

    monkeypatch.setattr("agent.stock_selector.get_candidate_stocks", fake_get_candidates)
    sa._ai_selection_score("2330", cfg={"pick_top_n": 5, "sector_exposure_cap": 0.6})
    assert captured_cfg["sector_exposure_cap"] is None


# ── analyze_stock 早退路徑：不存在的股票，不呼叫 LLM ────────────────
def test_analyze_stock_unknown_id_short_circuits(monkeypatch):
    # analyze_stock() 開頭會呼叫 ensure_execution_log_table()（確保kind欄位存在），
    # 這一步本身要連DB，早退測試的本意是「不需要DB」，所以連這步也要mock掉
    # （2026-07-17 Neon資料傳輸配額用盡事故才讓這個既有的測試隔離漏洞浮現，
    # 不是這次改動造成的回歸）。
    monkeypatch.setattr(sa, "ensure_execution_log_table", lambda: None)
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
