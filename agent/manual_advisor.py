"""
agent/manual_advisor.py

我的持倉（手動建倉）每日 AI 波段建議。

與 AI 部位的差別：
  - AI 部位（source='ai'）：符合出場規則 → 系統自動平倉
  - 手動持倉（source='manual'）：**只建議、不自動平倉**，由使用者自行決定

判斷邏輯全部沿用 agent/strategy.py（單一策略中樞原則）：
  - 賣出：decide_exit() 的 12 條波段出場規則
  - 加碼：賺錢中 + 回踩 MA20 附近 + 趨勢未壞（MACD>0 且 MA5>MA20）
  - 其餘：續抱（附 RSI 與距停損/停利百分比）

結果寫回 positions.last_advice / advice_date，並回傳彙總文字供 Telegram。
"""
from __future__ import annotations
from datetime import date

from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from database.connection import get_session
from agent.strategy import decide_exit, STRATEGY
from agent.portfolio import _recent_rows


def _build_extra(history: list[dict]) -> dict:
    """從近日資料組出 decide_exit 需要的 extra dict。"""
    today_r = history[-1]
    prev_r = history[-2] if len(history) >= 2 else {}
    n = STRATEGY.get("volume_avg_days", 20)
    vols = [h["volume"] for h in history[-(n + 1):-1] if h.get("volume")]
    return {
        "k":              today_r.get("k_value"),
        "d":              today_r.get("d_value"),
        "k_prev":         prev_r.get("k_value"),
        "d_prev":         prev_r.get("d_value"),
        "macd_hist":      today_r.get("macd_hist"),
        "macd_hist_prev": prev_r.get("macd_hist"),
        "open":           today_r.get("open"),
        "high":           today_r.get("high"),
        "low":            today_r.get("low"),
        "volume":         today_r.get("volume"),
        "avg_volume":     (sum(vols) / len(vols)) if vols else None,
    }


def advise_manual_positions(target_date: date = None) -> str | None:
    """
    對所有 open 的手動持倉產生建議，寫回 DB。
    回傳彙總文字（只含賣出/加碼建議；全部續抱回傳 None）。
    """
    if target_date is None:
        target_date = date.today()

    with get_session() as s:
        rows = s.execute(text("""
            SELECT p.id, p.stock_id, st.stock_name, p.entry_date, p.entry_price
            FROM positions p JOIN stocks st ON st.stock_id = p.stock_id
            WHERE p.source = 'manual' AND p.status = 'open'
            ORDER BY p.entry_date
        """)).fetchall()

    if not rows:
        return None

    logger.info(f"=== 我的持倉 AI 建議：{len(rows)} 筆 ===")
    alerts = []

    for pid, sid, name, entry_date, entry_price in rows:
        entry_price = float(entry_price)

        with get_session() as s:
            history = _recent_rows(s, sid, target_date, n=40)
            if not history:
                logger.warning(f"  {sid} 無價格資料，跳過")
                continue

            # 持有交易日數與期間最高價（手動倉沒有 peak_price 追蹤，即時算）
            hold_days = s.execute(text("""
                SELECT COUNT(*) FROM daily_prices
                WHERE stock_id = :sid AND trade_date > :ed AND trade_date <= :td
            """), {"sid": sid, "ed": entry_date, "td": target_date}).scalar() or 0
            peak = s.execute(text("""
                SELECT MAX(close) FROM daily_prices
                WHERE stock_id = :sid AND trade_date >= :ed AND trade_date <= :td
            """), {"sid": sid, "ed": entry_date, "td": target_date}).scalar()

        today_r = history[-1]
        close = today_r["close"]
        ma5, ma20 = today_r.get("ma5"), today_r.get("ma20")
        peak = max(float(peak) if peak else close, entry_price, close)
        gain = close / entry_price - 1

        # 1) 賣出判斷（沿用 12 條出場規則）
        should_exit, reason = decide_exit(
            entry_price, peak, close, ma5, ma20, hold_days,
            extra=_build_extra(history), history=history,
        )

        if should_exit:
            advice = f"⚠️ 建議賣出：{reason}（損益 {gain*100:+.1f}%）"
            alerts.append(f"  {sid} {name}：{advice}")
        else:
            # 2) 加碼判斷：賺錢中 + 回踩 MA20 ±3% + 趨勢未壞
            add_ok = (
                gain > 0
                and ma20 and abs(close - ma20) / ma20 <= 0.03
                and (today_r.get("macd_hist") or 0) > 0
                and ma5 and ma5 > ma20
            )
            if add_ok:
                advice = (f"➕ 可考慮加碼：回踩月線（收 {close:.2f}／MA20 {ma20:.2f}），"
                          f"趨勢未壞（損益 {gain*100:+.1f}%）")
                alerts.append(f"  {sid} {name}：{advice}")
            else:
                # 3) 續抱：附距停損/停利
                dist_sl = (close / (entry_price * (1 - STRATEGY["stop_loss"])) - 1) * 100
                dist_tp = (entry_price * (1 + STRATEGY["take_profit"]) / close - 1) * 100
                advice = (f"✅ 續抱（損益 {gain*100:+.1f}%，"
                          f"距停損 -{dist_sl:.1f}%，距停利 +{dist_tp:.1f}%）")

        with get_session() as s:
            s.execute(text("""
                UPDATE positions SET last_advice = :adv, advice_date = :dt
                WHERE id = :pid
            """), {"adv": advice, "dt": target_date, "pid": pid})

        logger.info(f"  {sid} {name}: {advice}")

    return "\n".join(alerts) if alerts else None
