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


def evaluate_watchlist(target_date: date = None) -> str | None:
    if target_date is None:
        from config.settings import tw_today
        target_date = tw_today()

    with get_session() as s:
        stocks = s.execute(text("""
            SELECT w.stock_id, st.stock_name, w.target_price
            FROM user_watchlist w JOIN stocks st ON st.stock_id = w.stock_id
            ORDER BY w.stock_id
        """)).fetchall()

    if not stocks:
        return None

    logger.info(f"=== 追蹤清單買點判斷：{len(stocks)} 檔 ===")
    green_alerts = []

    for sid, name, target_price in stocks:
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
            invest_streak3 = s.execute(text("""
                SELECT COUNT(*) FROM (
                    SELECT invest_net FROM institutional_trading
                    WHERE stock_id = :sid AND trade_date <= :td
                    ORDER BY trade_date DESC LIMIT 3
                ) t3 WHERE invest_net > 0
            """), {"sid": sid, "td": target_date}).scalar() == 3

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

        at_target = target_price is not None and close <= float(target_price)
        target_txt = "、已到目標價" if at_target else ""

        if score >= GREEN_THRESHOLD:
            signal = f"🟢 買點浮現（{score:.1f}分）：{'、'.join(hits)}{target_txt}"
            green_alerts.append(f"  {sid} {name}：{signal}｜現價 {close:.2f}")
        elif score >= YELLOW_THRESHOLD:
            signal = f"🟡 接近買點（{score:.1f}分）：{'、'.join(hits)}{target_txt}"
        else:
            rsi_txt = f"RSI {float(rsi):.0f}" if rsi is not None else "RSI —"
            signal = f"⚪ 觀望（現價 {close:.2f}，{rsi_txt}）{target_txt}"

        with get_session() as s:
            s.execute(text("""
                UPDATE user_watchlist SET last_signal = :sig, signal_date = :dt
                WHERE stock_id = :sid
            """), {"sig": signal, "dt": trade_date, "sid": sid})

        logger.info(f"  {sid} {name}: {signal}")

    return "\n".join(green_alerts) if green_alerts else None
