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
    assert "不在每日選股候選池內" in user_p


def test_prompt_without_ai_score_omits_section():
    # ai_score=None（預設值）：不加這段，舊呼叫方式（沒傳這個參數）不受影響
    _, user_p = sa._build_synthesis_prompt(
        "2330", _basic(), {}, None, {"bull": True, "ok": False}, {"ok": False}, [])
    assert "AI選股系統" not in user_p


# ── 2026-07-22 國巨事件後的修正：措辭/乖離/接刀紀律/籌碼量級 ──────────────
def _crashed_basic():
    # 國巨型態：收盤670遠低於MA20 927（月線下-27.7%），暴跌接刀
    return dict(stock_name="國巨*", industry="電子零組件業", trade_date=date(2026, 7, 21),
               close=670.0, change_pct=6.35, rsi14=35.7, macd_hist=-51.0,
               ma5=714.6, ma20=927.15, ma60=710.18, signal_ma_cross=0, signal_breakout=0)


def test_prompt_shows_ma_deviation_for_falling_knife():
    # 舊prompt完全沒把MA值給LLM看，暴跌股在月線下-27.7%這種訊號看不到
    _, user_p = sa._build_synthesis_prompt(
        "2327", _crashed_basic(), {"rs20": 0.0124, "stack_days": 0.0}, 38.9,
        {"bull": True, "ok": True, "close": 102.5, "ma60": 101.0}, {"ok": False}, [])
    assert "跌破月線MA20" in user_p
    assert "乖離-27.7%" in user_p or "乖離-27.6%" in user_p


def test_prompt_rs20_wording_is_unambiguous_for_weak_stock():
    # rs20=0.0124 舊寫法「全市場第1百分位」會被讀成「第一名=最強」，改成明講「極弱」
    _, user_p = sa._build_synthesis_prompt(
        "2327", _crashed_basic(), {"rs20": 0.0124, "stack_days": 0.0}, 38.9,
        {"bull": True, "ok": False}, {"ok": False}, [])
    assert "贏過全市場1%的股票" in user_p
    assert "極弱" in user_p
    assert "第 1 百分位" not in user_p   # 不再用會被讀反的舊措辭


def test_prompt_flags_broken_trend_structure():
    # stack_days=0 時舊版整條靜默省略，讓「趨勢已破」看不出來
    _, user_p = sa._build_synthesis_prompt(
        "2327", _crashed_basic(), {"rs20": 0.0124, "stack_days": 0.0}, 38.9,
        {"bull": True, "ok": False}, {"ok": False}, [])
    assert "非多頭排列" in user_p


def test_prompt_shows_20day_net_not_just_streak():
    # 籌碼要給近20日淨額，避免「連買1日」蓋過「近20日狂賣」
    inst = {"ok": True, "invest_streak_days": 1, "invest_streak_lots": 217.0,
            "foreign_streak_days": 0, "foreign_streak_lots": 0.0,
            "invest_total_lots": -14500.0, "foreign_total_lots": -8200.0,
            "latest_date": date(2026, 7, 21)}
    _, user_p = sa._build_synthesis_prompt(
        "2327", _crashed_basic(), {"rs20": 0.0124}, 38.9,
        {"bull": True, "ok": False}, {"ok": False}, [], None, )
    # 用有 inst 的版本
    _, user_p = sa._build_synthesis_prompt(
        "2327", _crashed_basic(), {"rs20": 0.0124}, 38.9,
        {"bull": True, "ok": False}, inst, [])
    assert "投信近20日淨賣超-14500張" in user_p
    assert "連買1日" in user_p


def test_system_prompt_has_falling_knife_discipline():
    sys_p, _ = sa._build_synthesis_prompt(
        "2327", _crashed_basic(), {}, None, {"bull": True, "ok": False}, {"ok": False}, [])
    assert "接刀" in sys_p
    assert "不應該給 positive" in sys_p


# ── 關鍵價位 _price_levels（2026-07-22，使用者要求補「需要留意的價位」）─────────
def _fake_session_returning(rows):
    from contextlib import contextmanager

    class _Res:
        def __init__(self, r): self._r = r
        def fetchall(self): return self._r

    class _Sess:
        def __init__(self, r): self._r = r
        def execute(self, *a, **k): return _Res(self._r)

    @contextmanager
    def _cm():
        yield _Sess(rows)
    return _cm


def test_price_levels_classifies_support_and_resistance(monkeypatch):
    # 近期 high/low：max high≈760、min low≈620；現價670
    rows = [(760.0, 700.0), (745.0, 628.0), (740.0, 620.0)] * 8   # 24筆，>=20
    monkeypatch.setattr(sa, "get_session", _fake_session_returning(rows))
    basic = {"close": 670.0, "ma5": 714.6, "ma20": 927.15, "ma60": 710.18}
    lv = sa._price_levels("2327", basic)
    assert lv["ok"] is True
    # 壓力全部 > 現價，支撐全部 < 現價
    assert all(r["price"] >= 670 for r in lv["resistances"])
    assert all(s["price"] < 670 for s in lv["supports"])
    # 壓力依「最靠近現價」排序（遞增）；季線710.18 應是最近的壓力
    assert lv["resistances"] == sorted(lv["resistances"], key=lambda x: x["price"])
    assert abs(lv["resistances"][0]["price"] - 710.18) < 5
    # dist_pct 方向正確：壓力為正、支撐為負
    assert lv["resistances"][0]["dist_pct"] > 0
    assert lv["supports"][0]["dist_pct"] < 0


def test_price_levels_merges_nearby_levels(monkeypatch):
    # 季線710.18 與 近期高710 很接近（<1.5%），應合併成同一關卡（標籤用＋串）
    rows = [(711.0, 620.0)] * 20
    monkeypatch.setattr(sa, "get_session", _fake_session_returning(rows))
    basic = {"close": 670.0, "ma5": None, "ma20": None, "ma60": 710.18}
    lv = sa._price_levels("2327", basic)
    merged = [r for r in lv["resistances"] if "＋" in r["label"]]
    assert merged, "相近的季線與近期高應合併成一個關卡"


def test_prompt_includes_key_levels_section(monkeypatch):
    levels = {"ok": True, "close": 670.0,
              "resistances": [{"label": "季線MA60", "price": 710.18, "dist_pct": 6.0}],
              "supports": [{"label": "近20日低", "price": 628.22, "dist_pct": -6.2}]}
    _, user_p = sa._build_synthesis_prompt(
        "2327", _crashed_basic(), {"rs20": 0.0124}, 38.9,
        {"bull": True, "ok": False}, {"ok": False}, [], None, levels)
    assert "關鍵價位" in user_p
    assert "季線MA60 710.18" in user_p
    assert "近20日低 628.22" in user_p


def test_system_prompt_requires_levels_and_invalidation():
    sys_p, _ = sa._build_synthesis_prompt(
        "2327", _crashed_basic(), {}, None, {"bull": True, "ok": False}, {"ok": False}, [])
    assert "key_levels" in sys_p
    assert "invalidation" in sys_p


# ── NaN 防呆（2026-07-22 線上實錘：顯示成「贏過nan%（極強）」）──────────────
def test_num_helper_rejects_nan_and_none():
    assert sa._num(float("nan")) is None
    assert sa._num(None) is None
    assert sa._num("abc") is None
    assert sa._num(0.5) == 0.5
    assert sa._num("3.5") == 3.5


def test_prompt_does_not_print_nan_for_missing_rs20():
    # rs20=NaN 時，舊寫法會印出「贏過全市場nan%」且強弱判斷掉到「極強」（最弱→最強）
    nan = float("nan")
    _, user_p = sa._build_synthesis_prompt(
        "2327", _crashed_basic(), {"rs20": nan, "stack_days": nan}, nan,
        {"bull": True, "ok": False}, {"ok": False}, [])
    assert "nan" not in user_p.lower()
    assert "相對強度RS20：資料不足" in user_p
    assert "極強" not in user_p


def test_price_levels_ignores_nan_moving_averages(monkeypatch):
    nan = float("nan")
    rows = [(760.0, 620.0)] * 20
    monkeypatch.setattr(sa, "get_session", _fake_session_returning(rows))
    basic = {"close": 670.0, "ma5": nan, "ma20": nan, "ma60": 710.18}
    lv = sa._price_levels("2327", basic)
    assert lv["ok"] is True
    labels = [x["label"] for x in lv["resistances"] + lv["supports"]]
    assert not any("5日線" in l or "月線" in l for l in labels)   # NaN 均線不該出現
    assert any("季線" in l for l in labels)                        # 正常的季線要在
    assert all(x["price"] == x["price"] for x in lv["resistances"] + lv["supports"])


# ── 引用驗證 _synthesis_grounding_flags ──────────────────────────────
def test_grounding_flags_fabricated_number():
    inst = {"ok": True, "invest_streak_days": 1, "invest_streak_lots": 217.0,
            "foreign_streak_days": 0, "foreign_streak_lots": 0.0,
            "invest_total_lots": -14500.0, "foreign_total_lots": -8200.0}
    parsed = {"bull_points": [{"point": "投信近20日買超99999張顯示認同"}],
              "bear_points": []}
    flags = sa._synthesis_grounding_flags(
        _crashed_basic(), {"rs20": 0.0124}, 38.9, inst, None, parsed)
    assert len(flags) == 1
    assert 99999.0 in flags[0]["numbers"]


def test_grounding_does_not_flag_legit_numbers_or_window_label():
    inst = {"ok": True, "invest_streak_days": 1, "invest_streak_lots": 217.0,
            "foreign_streak_days": 0, "foreign_streak_lots": 0.0,
            "invest_total_lots": -14500.0, "foreign_total_lots": -8200.0}
    # 全部是真實數字 + 「近20日」時間窗標籤（不該被誤判）
    parsed = {"bull_points": [{"point": "月營收年增38.9%"}],
              "bear_points": [{"point": "投信近20日淨賣14500張、乖離月線-27.7%"}]}
    flags = sa._synthesis_grounding_flags(
        _crashed_basic(), {"rs20": 0.0124}, 38.9, inst, None, parsed)
    assert flags == []


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
