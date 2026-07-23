"""
Streamlit 頁面渲染測試（2026-07-23 新增）——補上先前完全缺失的那一層。

為什麼需要：`_st` 變數覆蓋那個 bug（強弱標籤命名撞到存 stages 的字典）讓整個個股
分析頁在線上炸掉，但當時 275 個測試全過——因為它們測的都是「函式邏輯」，沒有任何
一個真的把 Streamlit 腳本跑起來渲染頁面。之前的「啟動冒煙測試」也只確認 server
起得來（HTTP 200），腳本其實是等瀏覽器連上才執行的，所以什麼都沒驗到。

這裡用 Streamlit 官方的 AppTest 真的執行 app.py：跳過登入（直接塞 session_state）、
逐頁切換、斷言沒有未捕捉例外。個股分析頁另外塞一份完整的 sa_result，確保
「有分析結果時」的渲染路徑（關鍵價位/多空論點/引用驗證那些卡片）也真的被執行到——
那正是 _st bug 出事的地方。

註：需要 DB（頁面會查資料），連不上就 skip。
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")

PAGES = ["📊 首頁", "📦 持倉追蹤", "🔖 追蹤清單", "🔎 個股分析", "🎯 練習軌", "🔥 族群輪動",
         "🏦 法人動向", "📉 個股走勢", "🔄 歷史績效", "📰 市場情報", "🧠 聰明資金", "🔍 決策軌跡"]


@pytest.fixture(scope="module")
def user():
    try:
        from database.connection import get_session
        from sqlalchemy import text
        with get_session() as s:
            row = s.execute(text(
                "SELECT user_id, username, role FROM users ORDER BY user_id LIMIT 1")).fetchone()
    except Exception as e:
        pytest.skip(f"DB 無法連線，跳過頁面渲染測試：{e}")
    if row is None:
        pytest.skip("users 表沒有資料，跳過頁面渲染測試")
    return {"user_id": row[0], "username": row[1], "display_name": row[1], "role": row[2]}


def _app(user, **session):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(APP_PATH, default_timeout=180)
    at.session_state["auth_user"] = user
    for k, v in session.items():
        at.session_state[k] = v
    return at


def _fake_sa_result():
    """比照 analyze_stock() 的回傳結構（含 2026-07-22 新增的 price_levels 段），
    讓「有分析結果」的渲染路徑真的被執行到。"""
    def stage(summary, payload):
        return {"summary": summary, "payload": payload, "sources": None,
                "status": "ok", "model_calls": 0, "tokens_in": 0, "tokens_out": 0}

    return {
        "ok": True, "run_id": "test-run-id-0000", "error": None,
        "stages": {
            "stock_context": stage("國巨*（電子零組件業）收盤 703.0 +4.93%", {
                "basic": {"stock_name": "國巨*", "industry": "電子零組件業",
                          "trade_date": date(2026, 7, 22), "close": 703.0, "change_pct": 4.93,
                          "rsi14": 39.1, "macd_hist": -20.0, "ma5": 695.6, "ma20": 913.0,
                          "ma60": 716.98, "signal_ma_cross": 0, "signal_breakout": 0},
                # rs20 用 NaN：重演線上「贏過nan%（極強）」那個 bug 的資料條件
                "trend": {"rs20": float("nan"), "stack_days": 0.0, "invest_streak": 2.0},
                "rev_yoy": 38.9}),
            "market_regime": stage("大盤多頭", {"ok": True, "bull": True, "ma60": 101.0,
                                                "close": 102.5, "stock_id": "0050"}),
            "ai_selection_score": stage("不在候選池內", {
                "ok": True, "in_universe": False, "veto_reason": None, "score": None,
                "rank": None, "total_candidates": 252, "percentile_rank": None,
                "would_make_top_n": False}),
            "institutional_flow": stage("投信連買2日", {
                "ok": True, "invest_streak_days": 2, "invest_streak_lots": 398.0,
                "foreign_streak_days": 0, "foreign_streak_lots": 0.0,
                "invest_total_lots": -15387.0, "foreign_total_lots": -8200.0,
                "latest_date": date(2026, 7, 22), "days": []}),
            "news_context": stage("近30日相關新聞 1 則", {"news": [
                {"date": date(2026, 7, 14), "type": "news", "sentiment": "negative",
                 "title": "國巨*再爆違約交割", "url": "https://example.com/x"}]}),
            "price_levels": stage("上方壓力 3 個｜下方支撐 3 個", {
                "ok": True, "close": 703.0,
                "resistances": [{"label": "季線MA60", "price": 716.98, "dist_pct": 2.0},
                                {"label": "月線MA20", "price": 913.0, "dist_pct": 29.9}],
                "supports": [{"label": "5日線", "price": 695.6, "dist_pct": -1.1},
                             {"label": "近20日低", "price": 608.0, "dist_pct": -13.5}]}),
            "synthesis": stage("判讀：negative", {
                "raw": "{}",
                "parsed": {
                    "verdict": "negative", "verdict_reason": "跌破均線且有違約交割",
                    "summary": "趨勢偏空，不建議進場。",
                    "bull_points": [{"point": "月營收年增+38.9%", "evidence_fields": ["rev_yoy"]}],
                    "bear_points": [{"point": "RSI 39.1 偏弱", "evidence_fields": ["rsi14"]}],
                    "key_levels": ["季線716.98為上方壓力", "近20日低608為關鍵支撐"],
                    "invalidation": "若帶量站回716.98則轉中性",
                    "data_gaps": ["相對強度資料不足"],
                },
                "grounding_flags": [{"side": "bull_points", "point": "投信買超99999張",
                                     "numbers": [99999.0]}],
                "llm_errors": []}),
        },
    }


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_without_exception(user, page):
    at = _app(user)
    at.run()
    assert not at.exception, f"首次載入就出錯：{[e.value for e in at.exception]}"
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, f"「{page}」渲染失敗：{[e.value for e in at.exception]}"


def test_stock_analysis_page_renders_full_result(user):
    """個股分析「已有結果」的完整渲染路徑——_st 覆蓋那個 bug 就是死在這條路上。"""
    at = _app(user, sa_result=_fake_sa_result(), sa_sid="2327")
    at.run()
    assert not at.exception
    at.sidebar.radio[0].set_value("🔎 個股分析").run()
    assert not at.exception, f"個股分析渲染失敗：{[e.value for e in at.exception]}"

    body = " ".join(m.value for m in at.markdown)
    assert "關鍵價位" in body          # 📏 卡片有出現
    assert "716.98" in body            # 程式算的壓力價位有顯示
    assert "608" in body               # 支撐價位有顯示
    assert "推翻" in body              # 🔄 invalidation 卡片
    assert "nan" not in body.lower()   # NaN 不可以漏到畫面上
