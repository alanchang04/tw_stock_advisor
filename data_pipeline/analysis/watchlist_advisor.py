"""
data_pipeline/analysis/watchlist_advisor.py

追蹤清單每日買點判斷（波段視角，數週~數月）。

訊號與權重沿用 agent/strategy.py 的 STRATEGY（單一策略中樞）：
  - 均線黃金交叉 (signal_ma_cross)      → w_ma_cross
  - 突破 20 日高 (signal_breakout)      → w_breakout
  - MACD 柱 > 0                         → w_macd_pos
  - 近 5 日三大法人淨買超               → w_inst_buy
  - 投信連 3 日買超                     → +1.5（聰明資金訊號）
  - RSI 50~65 甜蜜帶                    → w_rsi_sweet
  - 現價 ≤ 目標價（若有設）             → 額外標註

分數 ≥ 4.0 → 🟢 買點浮現；≥ 2.0 → 🟡 接近買點；否則 ⚪ 觀望。
結果寫回 user_watchlist.last_signal / signal_date，回傳 🟢 彙總（Telegram 用）。
"""
from __future__ import annotations
from datetime import date

from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from database.connection import get_session
from agent.strategy import STRATEGY

INVEST_STREAK_BONUS = 1.5   # 投信連 3 日買超加分
GREEN_THRESHOLD  = 4.0
YELLOW_THRESHOLD = 2.0
# 同 smart_money.py 的量體下限修正：純「連3天」不設量體下限會被雜訊觸發
# （曾實測 3 天總共只買 3 張的股票也算「連買」）。30張 ≈ smart_money 100張/15天
# 門檻的等比例縮小版（3天窗口）。
MIN_STREAK_TOTAL_LOTS = 30


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

    # 每支股票的基礎訊號只算一次（多清單共用）
    stock_ids = sorted({r[1] for r in items})
    names = {r[1]: r[2] for r in items}
    logger.info(f"=== 追蹤清單買點判斷：{len(items)} 項（{len(stock_ids)} 檔不重複）===")
    base_signals: dict[str, dict] = {}
    green_alerts = []

    for sid in stock_ids:
        name = names[sid]
        target_price = None   # 目標價為清單項目層級，稍後逐項判斷
        with get_session() as s:
            # 最新技術指標 + 收盤
            row = s.execute(text("""
                SELECT p.trade_date, p.close,
                       t.rsi14, t.macd_hist, t.signal_ma_cross, t.signal_breakout
                FROM daily_prices p
                LEFT JOIN technical_indicators t
                    ON t.stock_id = p.stock_id AND t.trade_date = p.trade_date
                WHERE p.stock_id = :sid AND p.close > 0 AND p.trade_date <= :td
                ORDER BY p.trade_date DESC LIMIT 1
            """), {"sid": sid, "td": target_date}).fetchone()
            if row is None:
                continue
            trade_date, close, rsi, macd_hist, ma_cross, breakout = row
            close = float(close)

            # 近 5 交易日法人 + 投信近 3 日是否連買
            inst = s.execute(text("""
                SELECT COALESCE(SUM(total_net),0),
                       COUNT(*) FILTER (WHERE invest_net > 0)
                FROM (
                    SELECT total_net, invest_net FROM institutional_trading
                    WHERE stock_id = :sid AND trade_date <= :td
                    ORDER BY trade_date DESC LIMIT 5
                ) t5
            """), {"sid": sid, "td": target_date}).fetchone()
            inst_5d_net = float(inst[0] or 0)
            streak3 = s.execute(text("""
                SELECT COUNT(*) FILTER (WHERE invest_net > 0),
                       COALESCE(SUM(invest_net) FILTER (WHERE invest_net > 0), 0)
                FROM (
                    SELECT invest_net FROM institutional_trading
                    WHERE stock_id = :sid AND trade_date <= :td
                    ORDER BY trade_date DESC LIMIT 3
                ) t3
            """), {"sid": sid, "td": target_date}).fetchone()
            # 天數=3 且累計量達標才算「連買」；純天數會被雜訊觸發
            # （曾實測3天總共只買3張的股票也符合舊版「連3買」條件）
            invest_streak3 = (streak3[0] == 3
                              and float(streak3[1]) / 1000 >= MIN_STREAK_TOTAL_LOTS)

        # ── 計分 ────────────────────────────────────────────────
        score, hits = 0.0, []
        if ma_cross and float(ma_cross) > 0:
            score += STRATEGY["w_ma_cross"]; hits.append("均線黃金交叉")
        if breakout and float(breakout) > 0:
            score += STRATEGY["w_breakout"]; hits.append("突破20日高")
        if macd_hist is not None and float(macd_hist) > 0:
            score += STRATEGY["w_macd_pos"]; hits.append("MACD翻正")
        if inst_5d_net > 0:
            score += STRATEGY["w_inst_buy"]; hits.append("法人5日買超")
        if invest_streak3:
            score += INVEST_STREAK_BONUS; hits.append("投信連3買")
        if rsi is not None and 50 <= float(rsi) <= 65:
            score += STRATEGY["w_rsi_sweet"]; hits.append(f"RSI甜蜜帶({float(rsi):.0f})")

        base_signals[sid] = {"score": score, "hits": hits, "close": close,
                             "rsi": rsi, "trade_date": trade_date}

    # 逐清單項目寫回（目標價是項目層級，逐項判斷）
    for list_id, sid, name, target_price, list_name, username in items:
        base = base_signals.get(sid)
        if base is None:
            continue
        score, hits = base["score"], base["hits"]
        close, rsi, trade_date = base["close"], base["rsi"], base["trade_date"]

        at_target = target_price is not None and close <= float(target_price)
        target_txt = "、已到目標價" if at_target else ""

        if score >= GREEN_THRESHOLD:
            signal = f"🟢 買點浮現（{score:.1f}分）：{'、'.join(hits)}{target_txt}"
            green_alerts.append(f"  {sid} {name}：{signal}｜現價 {close:.2f}"
                                f"（{username}／{list_name}）")
        elif score >= YELLOW_THRESHOLD:
            signal = f"🟡 接近買點（{score:.1f}分）：{'、'.join(hits)}{target_txt}"
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
