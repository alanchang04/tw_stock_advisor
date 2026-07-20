"""
data_pipeline/analysis/watchlist_advisor.py

追蹤清單每日買點判斷（波段視角，數週~數月）。

2026-07-20修正真bug：原本的計分公式手動疊加 w_ma_cross/w_breakout/w_macd_pos/
w_inst_buy/w_rsi_sweet 這5個STRATEGY權重，但這幾個權重從2026-07-15策略重構後
就已經全部降到0（改用相對強度/多頭排列/投信連買等新因子），沒有人回頭改這裡——
扣掉唯一還有效的「投信連3買+1.5分」，分數永遠不夠格觸發🟡(2.0分)或🟢(4.0分)，
這個功能上線後事實上不可能發出任何買點訊號，不管股票好壞都一樣，一直卡在「⚪觀望」。

改用 agent.stock_analysis.get_full_scored_universe()+rank_in_universe()——跟每日
AI選股、個股隨選分析共用同一套完整評分邏輯（相對強度/多頭排列/投信連買/新進場/
月營收年增等），不再手動維護一份會過期的簡化版權重。買點判斷改用「在當日全市場
候選裡的百分位排名」而不是絕對分數（分數尺度會隨STRATEGY權重調整而變，百分位
排名才是跨時間穩定的判準）：
  會進入今日實際推薦名單(would_make_top_n) → 🟢 買點浮現
  百分位 ≥ YELLOW_PERCENTILE（前30%強）     → 🟡 接近買點
  被空方硬否決規則排除                       → 🔴 空方否決訊號
  其餘（含未過流動性/RSI等基礎門檻）         → ⚪ 觀望

結果寫回 watchlist_items.last_signal / signal_date，回傳 🟢 彙總（Telegram 用）。
"""
from __future__ import annotations
from datetime import date

from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from database.connection import get_session
from agent.strategy import STRATEGY
from agent.stock_analysis import get_full_scored_universe, rank_in_universe

YELLOW_PERCENTILE = 0.70   # 百分位 ≥ 這個門檻 → 🟡 接近買點（贏過全市場候選前30%強）


def evaluate_watchlist(target_date: date = None) -> str | None:
    if target_date is None:
        from config.settings import tw_today
        target_date = tw_today()

    # 多使用者多清單：撈全部清單項目，同一支股票只計算一次訊號
    with get_session() as s:
        items = s.execute(text("""
            SELECT i.list_id, i.stock_id, st.stock_name, i.target_price,
                   w.list_name, u.username
            FROM watchlist_items i
            JOIN watchlists w ON w.list_id = i.list_id
            JOIN users u      ON u.user_id = w.user_id
            JOIN stocks st    ON st.stock_id = i.stock_id
            ORDER BY i.stock_id
        """)).fetchall()

    if not items:
        return None

    stock_ids = sorted({r[1] for r in items})
    names = {r[1]: r[2] for r in items}
    logger.info(f"=== 追蹤清單買點判斷：{len(items)} 項（{len(stock_ids)} 檔不重複）===")

    try:
        universe = get_full_scored_universe(STRATEGY)
    except Exception as e:
        logger.warning(f"追蹤清單買點判斷：全市場評分計算失敗（略過本次）: {e}")
        return None

    # 現價/RSI：即使股票沒進候選池（未過流動性/RSI等基礎門檻）也要能顯示現況，
    # 不能只在有評分時才更新——用 DISTINCT ON 一次查完每檔的最新一筆
    with get_session() as s:
        px_rows = s.execute(text("""
            SELECT DISTINCT ON (p.stock_id) p.stock_id, p.trade_date, p.close, t.rsi14
            FROM daily_prices p
            LEFT JOIN technical_indicators t ON t.stock_id = p.stock_id AND t.trade_date = p.trade_date
            WHERE p.stock_id = ANY(:ids) AND p.close > 0 AND p.trade_date <= :td
            ORDER BY p.stock_id, p.trade_date DESC
        """), {"ids": stock_ids, "td": target_date}).fetchall()
    px_map = {r[0]: {"trade_date": r[1], "close": float(r[2]), "rsi": r[3]} for r in px_rows}

    # 每支股票的分數/排名只算一次（多清單共用）
    base_signals: dict[str, dict] = {}
    for sid in stock_ids:
        px = px_map.get(sid)
        if px is None:
            continue
        base_signals[sid] = {**px, "ai": rank_in_universe(sid, universe, STRATEGY)}

    # 逐清單項目寫回（目標價是項目層級，逐項判斷）
    green_alerts = []
    for list_id, sid, name, target_price, list_name, username in items:
        base = base_signals.get(sid)
        if base is None:
            continue
        close, rsi, trade_date, ai = base["close"], base["rsi"], base["trade_date"], base["ai"]

        at_target = target_price is not None and close <= float(target_price)
        target_txt = "、已到目標價" if at_target else ""

        if ai.get("veto_reason"):
            signal = f"🔴 空方否決訊號：{ai['veto_reason'].strip('；')}{target_txt}"
        elif ai.get("in_universe") and ai.get("would_make_top_n"):
            signal = (f"🟢 買點浮現（AI選股第{ai['rank']}/{ai['total_candidates']}名，"
                     f"贏過{ai['percentile_rank']*100:.0f}%候選股）{target_txt}")
            green_alerts.append(f"  {sid} {name}：{signal}｜現價 {close:.2f}"
                                f"（{username}／{list_name}）")
        elif ai.get("in_universe") and (ai.get("percentile_rank") or 0) >= YELLOW_PERCENTILE:
            signal = f"🟡 接近買點（贏過{ai['percentile_rank']*100:.0f}%候選股）{target_txt}"
        else:
            rsi_txt = f"RSI {float(rsi):.0f}" if rsi is not None else "RSI —"
            signal = f"⚪ 觀望（現價 {close:.2f}，{rsi_txt}）{target_txt}"

        with get_session() as s:
            s.execute(text("""
                UPDATE watchlist_items SET last_signal = :sig, signal_date = :dt
                WHERE list_id = :lid AND stock_id = :sid
            """), {"sig": signal, "dt": trade_date, "lid": list_id, "sid": sid})

    logger.info(f"  完成：{len(items)} 項已更新")
    return "\n".join(green_alerts) if green_alerts else None
