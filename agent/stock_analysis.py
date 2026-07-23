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


def _num(v):
    """轉成 float，None/非數字/NaN 一律回 None。

    2026-07-22 線上實錘：資料不足時 rs20 會是 NaN，而 NaN 跟任何數字比較都是 False、
    且 NaN 是 truthy——導致最弱的國巨被顯示成「相對強度 贏過nan%（極強）」（強弱判斷
    一路掉到最後的 else）。凡是要拿來比較大小或格式化的因子值，都先過這個函式。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f      # NaN != NaN


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
    # 近N日淨額合計（2026-07-22 新增）：只給「連買天數」會誤導——國巨案例投信近兩週
    # 狂賣、最後一天才勉強+217張，「投信連買1日」被LLM讀成「法人認同」。給整段淨額
    # 才看得出「連買1日」是狂賣後的一次反彈、還是真的持續買進。
    inv_total = round(sum(d["invest_lots"] for d in days_data), 1)
    frn_total = round(sum(d["foreign_lots"] for d in days_data), 1)
    return {
        "days": days_data, "ok": True,
        "invest_streak_days": inv_days, "invest_streak_lots": round(inv_lots, 1),
        "foreign_streak_days": frn_days, "foreign_streak_lots": round(frn_lots, 1),
        "invest_total_lots": inv_total, "foreign_total_lots": frn_total,
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


def _price_levels(stock_id: str, basic: dict, lookback: int = 60) -> dict:
    """
    量化計算「支撐/壓力關鍵價位」（2026-07-22 新增，使用者要求補上「需要留意的價位」）。
    純規則計算、不靠LLM（跟乖離/引用驗證同一個philosophy：數字要能查證，不能讓LLM掰）。
    給波段操作者「這檔現在該盯哪些價位」：
      - MA5/MA20(月線)/MA60(季線)：最常被當動態支撐/壓力的均線
      - 近20日、近lookback日的波段高低點：前高=壓力、前低=支撐
    每個價位標成支撐（低於現價）或壓力（高於現價），附距現價%。相近的價位（1.5%內）
    合併成同一個關卡，避免列一堆擠在一起的數字。
    """
    close = _num(basic.get("close"))
    if not close or close <= 0:
        return {"ok": False}
    with get_session() as s:
        rows = s.execute(text("""
            SELECT high, low FROM daily_prices
            WHERE stock_id = :sid AND close > 0 AND high > 0
            ORDER BY trade_date DESC LIMIT :n
        """), {"sid": stock_id, "n": lookback}).fetchall()
    highs = [x for x in (_num(r[0]) for r in rows) if x is not None]
    lows = [x for x in (_num(r[1]) for r in rows) if x is not None]

    cand: list[tuple[str, float]] = []
    for label, v in (("5日線", basic.get("ma5")), ("月線MA20", basic.get("ma20")),
                     ("季線MA60", basic.get("ma60"))):
        v = _num(v)       # NaN 是 truthy，不擋掉會被當成合法價位（比較又全 False → 靜默消失）
        if v:
            cand.append((label, v))
    if len(highs) >= 20:
        cand.append(("近20日高", max(highs[:20])))
        cand.append(("近20日低", min(lows[:20])))
    if highs:
        cand.append((f"近{len(highs)}日高", max(highs)))
        cand.append((f"近{len(lows)}日低", min(lows)))

    # 相近價位（1.5%內）合併成同一關卡，標籤用「＋」串起來
    cand.sort(key=lambda x: x[1])
    merged: list[tuple[list[str], float]] = []
    for label, v in cand:
        if merged and abs(v - merged[-1][1]) / close <= 0.015:
            merged[-1][0].append(label)
        else:
            merged.append(([label], v))

    def _fmt(labels, v):
        return {"label": "＋".join(labels), "price": round(v, 2),
                "dist_pct": round((v - close) / close * 100, 1)}

    supports = [_fmt(lbls, v) for lbls, v in merged if v < close]
    supports.sort(key=lambda x: -x["price"])          # 最靠近現價的支撐排前面
    resistances = [_fmt(lbls, v) for lbls, v in merged if v >= close]
    resistances.sort(key=lambda x: x["price"])        # 最靠近現價的壓力排前面
    return {"ok": True, "close": round(close, 2), "supports": supports, "resistances": resistances}


def _synthesis_grounding_flags(basic: dict, trend: dict, rev_yoy, inst: dict,
                               ai_score: dict, parsed: dict, levels: dict | None = None) -> list[dict]:
    """
    引用驗證（2026-07-22，比照選股引擎的 citation_check）：檢查 LLM 在 bull/bear
    論點裡帶單位（%/日/張）複述的數字，能不能在我們實際給它的資料裡找到對應——
    找不到的標記出來，提醒使用者這句話的數字可能是 LLM 掰的。

    這裡刻意自建 expected 集合（不重用 citation_check.expected_numbers），因為個股
    分析餵給 LLM 的數字比選股候選列更多（MA值/乖離/近20日淨額/連買天數/排名百分位），
    用選股那套較窄的 expected 會把這些合法數字誤判成幻覺。只驗「帶單位」的數字，
    裸數字（MA值、目標價、EPS——後兩者來自新聞用「元」不帶我們認的單位）不驗。
    """
    import re
    from agent.citation_check import extract_cited_numbers
    if not parsed:
        return []
    # 「近20日」「近30日」是時間窗標籤不是引用數字，先拿掉再抽，免得20/30被誤判成幻覺
    # （跟 citation_check「60日動能」窗口標籤同一類坑）
    _win_re = re.compile(r"近\s*\d+\s*日")
    close, ma20 = basic.get("close"), basic.get("ma20")
    expected: set[float] = set()

    def add(v, nd=1):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return
        if v == v:  # not NaN
            expected.add(round(v, nd))

    add(basic.get("rsi14")); add(basic.get("change_pct"), 2)
    if close and ma20:
        add((close - ma20) / ma20 * 100)   # 乖離月線%
    if trend.get("rs20") is not None:
        add(trend["rs20"] * 100, 0)         # 相對強度百分位
    add(trend.get("stack_days"), 0)
    add(rev_yoy)
    if inst.get("ok"):
        for k in ("invest_streak_days", "invest_streak_lots", "foreign_streak_days",
                  "foreign_streak_lots", "invest_total_lots", "foreign_total_lots"):
            add(inst.get(k), 0)
    if ai_score and ai_score.get("in_universe"):
        add(ai_score.get("rank"), 0); add(ai_score.get("total_candidates"), 0)
        if ai_score.get("percentile_rank") is not None:
            add(ai_score["percentile_rank"] * 100, 0)
    if levels and levels.get("ok"):
        for lv in (levels.get("supports") or []) + (levels.get("resistances") or []):
            add(lv.get("dist_pct"))      # 各關鍵價位距現價% 也是合法可引用的數字
    expected_abs = {abs(e) for e in expected}

    flagged = []
    for side in ("bull_points", "bear_points"):
        for pt in parsed.get(side) or []:
            txt = pt.get("point", "")
            bad = [v for v in extract_cited_numbers(_win_re.sub("", txt))
                   if not any(abs(abs(v) - e) <= 1.5 for e in expected_abs)]
            if bad:
                flagged.append({"side": side, "point": txt, "numbers": bad})
    return flagged


def _build_synthesis_prompt(stock_id: str, basic: dict, trend: dict, rev_yoy, regime: dict,
                            inst: dict, news: list[dict], ai_score: dict | None = None,
                            levels: dict | None = None) -> tuple[str, str]:
    ma_cross = {1: "黃金交叉", -1: "死亡交叉", 0: "無"}.get(basic["signal_ma_cross"], "無")
    breakout = {1: "突破壓力", -1: "跌破支撐", 0: "無"}.get(basic["signal_breakout"], "無")
    close = basic.get("close")
    ma5, ma20, ma60 = basic.get("ma5"), basic.get("ma20"), basic.get("ma60")
    # 乖離月線（2026-07-22 新增）：國巨案例收盤670 vs MA20 927＝月線下方-27.7%（暴跌接刀），
    # 但舊prompt根本沒把MA值給LLM看，LLM只看到「當日+6.35%」誤讀成強勢。這裡明算乖離。
    dev20 = ((close - ma20) / ma20 * 100) if (close and ma20) else None
    ma_pos = []
    if close and ma20:
        ma_pos.append(f"{'站上' if close >= ma20 else '跌破'}月線MA20"
                      + (f"（乖離{dev20:+.1f}%）" if dev20 is not None else ""))
    if close and ma60:
        ma_pos.append(f"{'站上' if close >= ma60 else '跌破'}季線MA60")
    lines = [
        f"【{stock_id} {basic['stock_name']}】產業：{basic['industry']}",
        f"收盤 {close}｜當日漲跌 {basic.get('change_pct') or 0:+.2f}%｜"
        f"RSI {basic.get('rsi14') or 0:.1f}｜MACD柱{'正' if (basic.get('macd_hist') or 0) > 0 else '負'}",
        f"均線位階：MA5 {ma5}｜MA20 {ma20}｜MA60 {ma60}"
        + ("｜" + "、".join(ma_pos) if ma_pos else "")
        + f"｜單日訊號：均線{ma_cross}/突破{breakout}（單日訊號僅供進出場時機參考，非選股主因）",
    ]
    if ai_score and ai_score.get("ok"):
        if ai_score.get("in_universe"):
            lines.append(f"AI選股系統評分：第 {ai_score['rank']}/{ai_score['total_candidates']} 名"
                         f"（贏過全市場 {ai_score['percentile_rank']*100:.0f}% 候選股，"
                         f"套用跟每日自動推薦完全相同的評分公式）"
                         + ("｜目前分數足以進入每日實際推薦名單" if ai_score.get("would_make_top_n")
                            else "｜分數尚不足以進入每日實際推薦名單"))
        elif ai_score.get("veto_reason"):
            lines.append(f"AI選股系統：⚠️目前被空方硬否決規則排除（{ai_score['veto_reason'].strip('；')}）——這是強烈負面訊號")
        else:
            lines.append("AI選股系統：⚠️目前不在每日選股候選池內（未通過流動性/RSI/趨勢等基本篩選門檻）——代表這檔不符合系統的進場條件")
    tr = []
    rs = _num(trend.get("rs20"))
    if rs is not None:
        # 2026-07-22 修正措辭誤導：舊寫法「全市場第1百分位」會被讀成「第一名=最強」，
        # 但rs20=0.0124其實是「贏過全市場1%的股票＝最弱」。改成明講方向。
        rs_pct = rs * 100
        strength = "極弱" if rs_pct < 20 else ("偏弱" if rs_pct < 40 else ("中等" if rs_pct < 60 else ("偏強" if rs_pct < 80 else "極強")))
        tr.append(f"相對強度RS20：贏過全市場{rs_pct:.0f}%的股票（0=最弱,100=最強,此股屬{strength}）")
    else:
        # NaN/缺值不可硬印（會變成「贏過nan%」，且NaN比較全False會誤判成最強）
        tr.append("相對強度RS20：資料不足（不可據此論斷強弱）")
    _sd = _num(trend.get("stack_days"))
    if _sd:
        tr.append(f"多頭排列(MA5>MA20>MA60)連續{_sd:.0f}日")
    else:
        tr.append("⚠️目前非多頭排列（MA5>MA20>MA60不成立，趨勢結構未站穩）")
    _ry = _num(rev_yoy)
    if _ry is not None:
        tr.append(f"月營收年增{_ry:+.1f}%")
    lines.append("趨勢/基本面：" + "｜".join(tr))

    lines.append(f"大盤位階：{'多頭' if regime['bull'] else '空頭'}"
                 + (f"（0050還原後收盤{regime['close']:.2f} vs MA60 {regime['ma60']:.2f}）"
                    if regime.get("ok") else "（查無資料，保守視為多頭）"))

    if inst.get("ok"):
        # 2026-07-22：同時給「連買天數」與「近20日淨額合計」，避免LLM把狂賣後的
        # 單日反彈誤讀成法人認同（國巨案例：投信連買1日但近20日其實淨賣）。
        lines.append(
            f"籌碼（近{INSTITUTIONAL_LOOKBACK_DAYS}日，單位：張）："
            f"投信近{INSTITUTIONAL_LOOKBACK_DAYS}日淨{'買' if inst.get('invest_total_lots',0)>=0 else '賣'}超"
            f"{inst.get('invest_total_lots',0):+.0f}張（目前連買{inst['invest_streak_days']}日{inst['invest_streak_lots']:+.0f}張）"
            f"｜外資近{INSTITUTIONAL_LOOKBACK_DAYS}日淨{'買' if inst.get('foreign_total_lots',0)>=0 else '賣'}超"
            f"{inst.get('foreign_total_lots',0):+.0f}張（目前連買{inst['foreign_streak_days']}日{inst['foreign_streak_lots']:+.0f}張）"
            f"｜最新資料日{inst['latest_date']}")
    else:
        lines.append("籌碼：查無法人買賣超資料（不可假設無風險）")

    if news:
        lines.append("相關新聞/影音（近30日）：")
        for n in news:
            lines.append(f"  {n['date']}[{n['sentiment'] or '中性'}]《{n['title']}》")
    else:
        lines.append("相關新聞：近30日查無相關報導")

    # 關鍵價位（2026-07-22 新增）：程式算好的支撐/壓力，餵給LLM讓它的判斷有具體
    # 價位可講（進場/停損/目標要參考這些關卡），不是空泛講「偏空」。
    if levels and levels.get("ok"):
        def _lv_str(items):
            return "｜".join(f"{it['label']} {it['price']:.2f}（{it['dist_pct']:+.1f}%）" for it in items) or "—"
        lines.append(f"關鍵價位（程式計算，現價 {levels['close']:.2f}）：")
        lines.append(f"  上方壓力：{_lv_str(levels.get('resistances') or [])}")
        lines.append(f"  下方支撐：{_lv_str(levels.get('supports') or [])}")

    user_prompt = "\n".join(lines) + "\n\n請根據以上資料，對這檔股票做綜合分析，嚴格按指定JSON格式回覆。"
    system_prompt = """你是台股分析師，要對使用者指定的單一股票做綜合研判。你要幫做波段的
使用者判斷「現在這檔值不值得進場」，不是幫已經套牢的人找安慰——要務實、要看當下。

紀律（比照公司內部辯論標準）：
1. 每個論點必須引用上面資料的具體數值，禁止空泛形容（不可只寫「表現強勢」，要寫出
   具體數字）。引用數字時要照上面資料的原意，不可扭曲：例如「相對強度贏過全市場1%」
   代表它是最弱的1%，不是最強——看清楚方向再寫。
2. bull_points 與 bear_points 都要寫，即使你認為整體偏多/偏空，也要誠實列出對面
   立場最強的論點——不是各打五十大板，是要求你不能只講單一面向。
3. 若資料缺失（查無法人資料/查無新聞），要在 data_gaps 明確列出，不可假裝沒有風險。
4. verdict 是綜合判斷後的整體傾向，不是簡單多數決。**判斷時「當下的價格位階與籌碼」
   權重要高於「過去的營收/舊的分析師目標價」**——一家營收成長的好公司如果正在暴跌
   （見下方第6點），對想進場的人來說是接刀，不是機會。
5. 若上面有「AI選股系統評分」這行，那是系統每天自動選股用的同一套評分公式算出來的
   真實排名——分數/排名或「被硬否決/不在候選池」跟你自己的定性判斷如果衝突（例如
   系統把它排除但你覺得故事很好），要在分析裡明確指出這個落差並認真看待，不能忽略。
6. **接刀防呆（重要）**：若股價明顯跌破月線（乖離為較大負值）、RSI偏弱、或近期有
   違約交割/處置/警示等重大負面新聞——這些是「趨勢已壞、下跌未止」的訊號，即使基本面
   （營收年增）或舊目標價看起來漂亮，verdict 也不應該給 positive（頂多 neutral，多半
   應該 negative）。理由是「好公司≠現在該買」，暴跌中的股票要等止穩訊號。
7. **籌碼要看量級與趨勢，不是只看方向**：「投信連買1日+200張」在一檔近20日被法人淨賣
   幾千張的股票上，是狂賣後的單日反彈，不是「法人認同」。請比較「近20日淨額」與「目前
   連買」兩個數字後再論斷，小量級的單日訊號不可當成強力買盤。
8. **要給可操作的價位（重要）**：波段操作者需要知道「該盯哪些價位、什麼情況會改變判斷」。
   請用上面「關鍵價位」那幾個程式算好的支撐/壓力（不要自己另外編價位，要用列出來的），
   填寫 key_levels（該留意的支撐/壓力，含用途說明）與 invalidation（什麼價位或條件會
   讓你這個 verdict 失效，例如「若帶量站回月線XXX則翻中性/偏多」「若跌破支撐XXX則加速
   趕底」）。這兩欄要具體到價位數字，不能空泛。

回覆格式（只回JSON，不要其他文字）：
{
  "verdict": "positive|neutral|negative",
  "verdict_reason": "一句話說明整體判斷（30字以內）",
  "bull_points": [{"point": "具體論點", "evidence_fields": ["rs20","invest_streak"]}],
  "bear_points": [{"point": "具體論點", "evidence_fields": ["rsi14"]}],
  "key_levels": ["該留意的價位＋用途，例：季線710為關鍵支撐，跌破恐再下探；月線927為上方壓力"],
  "invalidation": "什麼價位/條件會推翻上面的 verdict（要具體到數字）",
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

    with run.stage("price_levels") as rec:
        levels = _price_levels(stock_id, basic)
        if levels.get("ok"):
            _r = levels.get("resistances") or []
            _s = levels.get("supports") or []
            rec.summary = (f"上方壓力 {len(_r)} 個"
                           + (f"（最近 {_r[0]['label']} {_r[0]['price']:.2f}）" if _r else "（無，已在近期高點上緣）")
                           + f"｜下方支撐 {len(_s)} 個"
                           + (f"（最近 {_s[0]['label']} {_s[0]['price']:.2f}）" if _s else "（無，已破近期低點）"))
        else:
            rec.summary = "價位資料不足"
        rec.payload = levels
        _record("price_levels", rec.summary, rec.payload)

    with run.stage("synthesis") as rec:
        from agent.llm_advisor import _ask, _parse_json
        system_prompt, user_prompt = _build_synthesis_prompt(
            stock_id, basic, trend, rev_yoy, regime, inst, news, ai_score, levels)
        raw = _ask(system_prompt, user_prompt, max_tokens=1400, rec=rec, json_mode=True)
        parsed = _parse_json(raw) if raw else None
        if parsed and not parsed.get("verdict"):
            parsed = None
        grounding = _synthesis_grounding_flags(basic, trend, rev_yoy, inst, ai_score, parsed, levels) if parsed else []
        _sfx = f"（🔍 {len(grounding)} 句引用數字查無對應）" if grounding else ""
        # 失敗要分清楚是「呼叫失敗（API/金鑰/額度/網路）」還是「有回應但解析不出來」——
        # 這兩種的處理方式完全不同，混用一句話會誤導（2026-07-23 線上實際踩到）
        _llm_errs = list(getattr(rec, "llm_errors", []) or [])
        if parsed:
            rec.summary = f"判讀：{parsed.get('verdict')}｜{parsed.get('verdict_reason', '')}{_sfx}"
        elif raw:
            rec.summary = f"LLM 有回應但輸出無法解析（回應長度 {len(raw)} 字）"
        else:
            rec.summary = "LLM 呼叫失敗" + (f"：{_llm_errs[0]}" if _llm_errs else "（無錯誤訊息）")
        rec.payload = {"raw": raw, "parsed": parsed, "grounding_flags": grounding,
                       "llm_errors": _llm_errs}
        _record("synthesis", rec.summary, rec.payload,
               sources=None)
        stages["synthesis"]["model_calls"] = rec.model_calls
        stages["synthesis"]["tokens_in"] = rec.tokens_in
        stages["synthesis"]["tokens_out"] = rec.tokens_out

    ok = stages["synthesis"]["payload"]["parsed"] is not None
    return {"ok": ok, "run_id": run.run_id,
           "error": None if ok else "AI 綜合判讀失敗（資料段落仍完整，可參考上面幾段）",
           "stages": stages}
