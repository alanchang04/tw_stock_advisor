"""
agent/stock_analysis.py

個股隨選分析——把「決策軌跡」的透明分段概念套用到單一股票的即時查詢。
使用者輸入一檔股票，逐段收集背景（技術/趨勢因子）→ 大盤位階 → 投信外資近期
買賣超 → 相關新聞 → 最後一次 LLM 綜合判讀，全程記錄成一個 execution_log run
（kind='stock_analysis'，跟每日 pipeline 的 run 分開列，不進決策軌跡頁的清單，
但用同一套 exec_log 基礎設施：耗時、payload、來源）。

沿用 SPEC_REASONING_LAYER 的紀律：LLM 輸出必須逐點引用具體資料欄位（evidence_
_fields），禁止空泛形容；資料缺失（法人落後/查無新聞）要明確告知使用者，
不能默默當作「沒有風險」。

用法：
    from agent.stock_analysis import analyze_stock
    result = analyze_stock("2330")     # 回傳 dict，含 run_id 與各段 summary/payload
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from database.connection import get_session
from agent.exec_log import ExecRun, ensure_execution_log_table
from agent.strategy import STRATEGY

INSTITUTIONAL_LOOKBACK_DAYS = 20


def _stock_basic(stock_id: str) -> dict | None:
    with get_session() as s:
        row = s.execute(text("""
            SELECT s.stock_name, i.name_zh,
                   p.trade_date, p.close, p.change_pct,
                   t.rsi14, t.macd_hist, t.ma5, t.ma20, t.ma60,
                   t.signal_ma_cross, t.signal_breakout
            FROM stocks s
            LEFT JOIN stock_industry_map m ON m.stock_id = s.stock_id
            LEFT JOIN industries i ON i.code = m.industry_code
            JOIN daily_prices p ON p.stock_id = s.stock_id
                AND p.trade_date = (SELECT MAX(trade_date) FROM daily_prices WHERE stock_id = s.stock_id)
            LEFT JOIN technical_indicators t ON t.stock_id = s.stock_id AND t.trade_date = p.trade_date
            WHERE s.stock_id = :sid
        """), {"sid": stock_id}).fetchone()
    if row is None:
        return None
    return dict(stock_name=row[0], industry=row[1] or "未分類", trade_date=row[2],
               close=float(row[3]) if row[3] is not None else None,
               change_pct=float(row[4]) if row[4] is not None else None,
               rsi14=float(row[5]) if row[5] is not None else None,
               macd_hist=float(row[6]) if row[6] is not None else None,
               ma5=float(row[7]) if row[7] is not None else None,
               ma20=float(row[8]) if row[8] is not None else None,
               ma60=float(row[9]) if row[9] is not None else None,
               signal_ma_cross=int(row[10]) if row[10] is not None else 0,
               signal_breakout=int(row[11]) if row[11] is not None else 0)


def _trend_factors(stock_id: str) -> dict:
    """相對強度/多頭排列/投信連買——與回測/每日選股共用同一份計算（見 stock_selector._live_factor_maps）。"""
    try:
        from agent.stock_selector import _live_factor_maps
        fmaps = _live_factor_maps(STRATEGY)
        return {
            "rs20": fmaps["rs20"].get(stock_id),
            "stack_days": fmaps["stack_days"].get(stock_id),
            "invest_streak": fmaps["invest_streak"].get(stock_id),
        }
    except Exception as e:
        logger.warning(f"趨勢因子計算失敗（略過）: {e}")
        return {"rs20": None, "stack_days": None, "invest_streak": None}


def get_full_scored_universe(cfg: dict = STRATEGY):
    """
    2026-07-20 新增：算一次「今天全市場、套用AI選股完整篩選+評分邏輯」的候選池
    （stock_selector.get_candidate_stocks → strategy.score_candidates），回傳依分數
    排序的完整 DataFrame（不是只有top_n）。給需要「對多檔股票分別查排名」的呼叫端
    （個股隨選分析、追蹤清單買點判斷）共用同一次計算，不要每檔股票各自重算一次
    全市場——那是同一份運算，重複算沒有意義，只會拖慢+浪費查詢。

    族群曝險上限（sector_exposure_cap）這裡刻意關閉：那是「目前投資組合裡這個族群
    是否已經滿了」的暫時性限制，會隨當天持倉狀態變動；查詢個股/清單分數要看的是
    「這檔股票本身的分數/排名」這種相對穩定的資訊，不該被組合層的暫時限制影響。
    """
    from agent.stock_selector import get_candidate_stocks
    raw_cfg = {**cfg, "sector_exposure_cap": None}
    return get_candidate_stocks([], top_n=99999, cfg=raw_cfg)


def rank_in_universe(stock_id: str, universe, cfg: dict = STRATEGY) -> dict:
    """
    純函式：從 get_full_scored_universe() 算好的候選池裡，查單一股票的分數/排名/
    百分位/是否被硬否決排除。回傳格式跟 _ai_selection_score() 一致（歷史相容）。
    """
    hard_excluded = {r["stock_id"]: r["hard_veto_reason"]
                     for r in (universe.attrs.get("hard_excluded") or [])}
    total = len(universe)
    if stock_id in hard_excluded:
        return {"ok": True, "in_universe": False, "veto_reason": hard_excluded[stock_id],
               "score": None, "rank": None, "total_candidates": total,
               "percentile_rank": None, "would_make_top_n": False}
    if universe.empty or stock_id not in universe["stock_id"].values:
        return {"ok": True, "in_universe": False, "veto_reason": None,
               "score": None, "rank": None, "total_candidates": total,
               "percentile_rank": None, "would_make_top_n": False}

    row = universe[universe["stock_id"] == stock_id].iloc[0]
    rank = int((universe["score"] >= row["score"]).sum())   # 1 = 全市場最高分
    return {
        "ok": True, "in_universe": True, "veto_reason": None,
        "score": float(row["score"]), "rank": rank, "total_candidates": total,
        "percentile_rank": round(1 - (rank - 1) / total, 3) if total else None,
        "would_make_top_n": rank <= cfg.get("pick_top_n", 5),
    }


def _ai_selection_score(stock_id: str, cfg: dict = STRATEGY) -> dict:
    """
    套用「每日AI選股」同一套候選篩選+評分邏輯，對單一股票查詢——只查一檔股票時的
    便利包裝（内部算一次全市場候選池）。要對多檔股票各自查詢時，改用
    get_full_scored_universe()算一次+rank_in_universe()逐檔查，避免每檔都重算
    一次全市場（見 data_pipeline/analysis/watchlist_advisor.py 的用法）。
    """
    try:
        universe = get_full_scored_universe(cfg)
    except Exception as e:
        logger.warning(f"AI選股評分計算失敗（略過）: {e}")
        return {"ok": False, "in_universe": False, "veto_reason": None,
               "score": None, "rank": None, "total_candidates": None,
               "percentile_rank": None, "would_make_top_n": None}
    return rank_in_universe(stock_id, universe, cfg)


def _revenue_yoy(stock_id: str) -> float | None:
    with get_session() as s:
        row = s.execute(text("""
            SELECT yoy_pct FROM monthly_revenue
            WHERE stock_id = :sid AND year_month = (SELECT MAX(year_month) FROM monthly_revenue)
        """), {"sid": stock_id}).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _institutional_flow(stock_id: str, days: int = INSTITUTIONAL_LOOKBACK_DAYS) -> dict:
    """
    近 N 日投信/外資/三大法人合計買賣超逐日明細 + 連買/連賣天數與累計量（張）。
    單位換算：DB 存股，此處統一換算成張（÷1000），避免股/張混淆的舊 bug 重演。
    """
    with get_session() as s:
        rows = s.execute(text("""
            SELECT trade_date, invest_net, foreign_net, total_net FROM institutional_trading
            WHERE stock_id = :sid
            ORDER BY trade_date DESC LIMIT :n
        """), {"sid": stock_id, "n": days}).fetchall()
    if not rows:
        return {"days": [], "ok": False}
    rows = list(reversed(rows))     # oldest → newest
    days_data = [{"trade_date": r[0], "invest_lots": float(r[1]) / 1000,
                 "foreign_lots": float(r[2]) / 1000, "total_lots": float(r[3]) / 1000} for r in rows]

    def _streak(key: str) -> tuple[int, float]:
        cnt, cum = 0, 0.0
        for d in reversed(days_data):
            v = d[key]
            if v > 0:
                cnt += 1
                cum += v
            else:
                break
        return cnt, cum

    inv_days, inv_lots = _streak("invest_lots")
    frn_days, frn_lots = _streak("foreign_lots")
    return {
        "days": days_data, "ok": True,
        "invest_streak_days": inv_days, "invest_streak_lots": round(inv_lots, 1),
        "foreign_streak_days": frn_days, "foreign_streak_lots": round(frn_lots, 1),
        "latest_date": days_data[-1]["trade_date"] if days_data else None,
    }


def _recent_news(stock_id: str, days: int = 30, limit: int = 5) -> list[dict]:
    with get_session() as s:
        rows = s.execute(text("""
            SELECT signal_date, signal_type, title, sentiment, url FROM market_signals
            WHERE signal_type IN ('news', 'youtube')
              AND related_stocks IS NOT NULL AND :sid = ANY(related_stocks)
              AND signal_date >= CURRENT_DATE - CAST(:d AS int)
            ORDER BY signal_date DESC LIMIT :lim
        """), {"sid": stock_id, "d": days, "lim": limit}).fetchall()
    return [{"date": r[0], "type": r[1], "title": r[2], "sentiment": r[3], "url": r[4]} for r in rows]


def _build_synthesis_prompt(stock_id: str, basic: dict, trend: dict, rev_yoy, regime: dict,
                            inst: dict, news: list[dict], ai_score: dict | None = None) -> tuple[str, str]:
    ma_cross = {1: "黃金交叉", -1: "死亡交叉", 0: "無"}.get(basic["signal_ma_cross"], "無")
    breakout = {1: "突破壓力", -1: "跌破支撐", 0: "無"}.get(basic["signal_breakout"], "無")
    lines = [
        f"【{stock_id} {basic['stock_name']}】產業：{basic['industry']}",
        f"收盤 {basic['close']}｜當日漲跌 {basic.get('change_pct') or 0:+.2f}%｜"
        f"RSI {basic.get('rsi14') or 0:.1f}｜MACD柱{'正' if (basic.get('macd_hist') or 0) > 0 else '負'}｜"
        f"單日訊號：均線{ma_cross}/突破{breakout}（僅供進出場時機參考，非選股主因）",
    ]
    if ai_score and ai_score.get("ok"):
        if ai_score.get("in_universe"):
            lines.append(f"AI選股系統評分：第 {ai_score['rank']}/{ai_score['total_candidates']} 名"
                         f"（贏過全市場 {ai_score['percentile_rank']*100:.0f}% 候選股，"
                         f"套用跟每日自動推薦完全相同的評分公式）"
                         + ("｜目前分數足以進入每日實際推薦名單" if ai_score.get("would_make_top_n")
                            else "｜分數尚不足以進入每日實際推薦名單"))
        elif ai_score.get("veto_reason"):
            lines.append(f"AI選股系統：目前被空方硬否決規則排除（{ai_score['veto_reason'].strip('；')}）")
        else:
            lines.append("AI選股系統：目前不在候選池內（未通過流動性/RSI等基本篩選門檻）")
    tr = []
    if trend.get("rs20") is not None:
        tr.append(f"相對強度 RS20 全市場第 {trend['rs20']*100:.0f} 百分位")
    if trend.get("stack_days"):
        tr.append(f"多頭排列(MA5>20>60)連續 {trend['stack_days']:.0f} 日")
    if rev_yoy is not None:
        tr.append(f"月營收年增 {rev_yoy:+.1f}%")
    lines.append("趨勢/基本面：" + ("｜".join(tr) if tr else "無明顯訊號"))

    lines.append(f"大盤位階：{'多頭' if regime['bull'] else '空頭'}"
                 + (f"（0050還原後收盤{regime['close']:.2f} vs MA60 {regime['ma60']:.2f}）"
                    if regime.get("ok") else "（查無資料，保守視為多頭）"))

    if inst.get("ok"):
        lines.append(f"籌碼（近{INSTITUTIONAL_LOOKBACK_DAYS}日，單位：張）：投信連買{inst['invest_streak_days']}日"
                     f"（累計{inst['invest_streak_lots']:+.0f}張）｜外資連買{inst['foreign_streak_days']}日"
                     f"（累計{inst['foreign_streak_lots']:+.0f}張）｜最新資料日{inst['latest_date']}")
    else:
        lines.append("籌碼：查無法人買賣超資料（不可假設無風險）")

    if news:
        lines.append("相關新聞/影音（近30日）：")
        for n in news:
            lines.append(f"  {n['date']}[{n['sentiment'] or '中性'}]《{n['title']}》")
    else:
        lines.append("相關新聞：近30日查無相關報導")

    user_prompt = "\n".join(lines) + "\n\n請根據以上資料，對這檔股票做綜合分析，嚴格按指定JSON格式回覆。"
    system_prompt = """你是台股分析師，要對使用者指定的單一股票做綜合研判。

紀律（比照公司內部辯論標準）：
1. 每個論點必須引用上面資料的具體數值，禁止空泛形容（不可只寫「表現強勢」，
   要寫「RS20全市場第X百分位」這種可查證的具體依據）。
2. bull_points 與 bear_points 都要寫，即使你認為整體偏多/偏空，也要誠實列出
   對面立場最強的論點——不是各打五十大板，是要求你不能只講單一面向。
3. 若資料缺失（查無法人資料/查無新聞），要在 data_gaps 明確列出，不可假裝沒有風險。
4. verdict 是綜合判斷後的整體傾向，不是簡單多數決。
5. 若上面有「AI選股系統評分」這行，那是系統每天自動選股用的同一套評分公式算出來的
   真實排名，不是另外編的——分數/排名跟你自己的定性判斷如果衝突（例如排名很後面但
   你覺得故事很好），要在分析裡明確指出這個落差，不能忽略不提。

回覆格式（只回JSON，不要其他文字）：
{
  "verdict": "positive|neutral|negative",
  "verdict_reason": "一句話說明整體判斷（30字以內）",
  "bull_points": [{"point": "具體論點", "evidence_fields": ["rs20","invest_streak"]}],
  "bear_points": [{"point": "具體論點", "evidence_fields": ["rsi14"]}],
  "data_gaps": ["資料缺失項目，沒有缺失則為空陣列"],
  "summary": "整體摘要（80字以內）"
}"""
    return system_prompt, user_prompt


def analyze_stock(stock_id: str) -> dict:
    """
    對單一股票做個股分析，逐段記錄進 execution_log（kind='stock_analysis'）。
    回傳 dict：{"ok": bool, "run_id": str, "error": str|None, "stages": {stage名: {...}}}
    stages 內容直接可供 Streamlit 渲染，不需重查 DB。
    """
    ensure_execution_log_table()   # 確保 kind 欄位存在（直接建構 ExecRun 不走 start_run()）
    run = ExecRun(kind="stock_analysis")
    stages: dict[str, dict] = {}

    def _record(name, summary, payload=None, sources=None):
        stages[name] = {"summary": summary, "payload": payload, "sources": sources,
                        "status": "ok", "model_calls": 0, "tokens_in": 0, "tokens_out": 0}

    with run.stage("stock_context") as rec:
        basic = _stock_basic(stock_id)
        if basic is None:
            rec.summary = f"查無股票代號 {stock_id}"
            _record("stock_context", rec.summary)
            return {"ok": False, "run_id": run.run_id, "error": f"查無股票代號 {stock_id}", "stages": stages}
        trend = _trend_factors(stock_id)
        rev_yoy = _revenue_yoy(stock_id)
        rec.summary = (f"{basic['stock_name']}（{basic['industry']}）"
                       f"收盤{basic['close']} {basic.get('change_pct') or 0:+.2f}%")
        rec.payload = {"basic": basic, "trend": trend, "rev_yoy": rev_yoy}
        _record("stock_context", rec.summary, rec.payload)

    with run.stage("market_regime") as rec:
        from agent.stock_selector import market_regime_detail
        regime = market_regime_detail()
        rec.summary = (f"大盤{'多頭' if regime['bull'] else '空頭'}"
                       + (f"（{regime['stock_id']} {regime['close']:.2f} vs MA60 {regime['ma60']:.2f}）"
                          if regime.get("ok") else "（資料不足，保守視為多頭）"))
        rec.payload = regime
        _record("market_regime", rec.summary, rec.payload)

    with run.stage("ai_selection_score") as rec:
        ai_score = _ai_selection_score(stock_id)
        if not ai_score.get("ok"):
            rec.summary = "AI選股評分計算失敗（略過，其他段落不受影響）"
        elif ai_score["in_universe"]:
            rec.summary = (f"AI選股評分：第{ai_score['rank']}/{ai_score['total_candidates']}名"
                           f"（贏過{ai_score['percentile_rank']*100:.0f}%候選股）")
        elif ai_score.get("veto_reason"):
            rec.summary = f"被空方硬否決規則排除：{ai_score['veto_reason'].strip('；')}"
        else:
            rec.summary = "目前不在候選池內（未通過基本篩選門檻）"
        rec.payload = ai_score
        _record("ai_selection_score", rec.summary, rec.payload)

    with run.stage("institutional_flow") as rec:
        inst = _institutional_flow(stock_id)
        if inst["ok"]:
            rec.summary = (f"投信連買{inst['invest_streak_days']}日({inst['invest_streak_lots']:+.0f}張)｜"
                           f"外資連買{inst['foreign_streak_days']}日({inst['foreign_streak_lots']:+.0f}張)")
        else:
            rec.summary = "查無法人買賣超資料"
        rec.payload = inst
        _record("institutional_flow", rec.summary, rec.payload)

    with run.stage("news_context") as rec:
        news = _recent_news(stock_id)
        rec.summary = f"近30日相關新聞/影音 {len(news)} 則" if news else "近30日查無相關新聞"
        rec.payload = {"news": news} if news else None
        _record("news_context", rec.summary, rec.payload)

    with run.stage("synthesis") as rec:
        from agent.llm_advisor import _ask, _parse_json
        system_prompt, user_prompt = _build_synthesis_prompt(
            stock_id, basic, trend, rev_yoy, regime, inst, news, ai_score)
        raw = _ask(system_prompt, user_prompt, max_tokens=1200, rec=rec, json_mode=True)
        parsed = _parse_json(raw) if raw else None
        if parsed and not parsed.get("verdict"):
            parsed = None
        rec.summary = (f"判讀：{parsed.get('verdict')}｜{parsed.get('verdict_reason', '')}"
                       if parsed else ("LLM 輸出無法解析" if raw else "LLM 呼叫失敗"))
        rec.payload = {"raw": raw, "parsed": parsed}
        _record("synthesis", rec.summary, rec.payload,
               sources=None)
        stages["synthesis"]["model_calls"] = rec.model_calls
        stages["synthesis"]["tokens_in"] = rec.tokens_in
        stages["synthesis"]["tokens_out"] = rec.tokens_out

    ok = stages["synthesis"]["payload"]["parsed"] is not None
    return {"ok": ok, "run_id": run.run_id,
           "error": None if ok else "AI 綜合判讀失敗（資料段落仍完整，可參考上面幾段）",
           "stages": stages}
