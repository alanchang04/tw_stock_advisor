"""
data_pipeline/analysis/smart_money.py

聰明資金追蹤：投信連買 × 統一ETF換股
- 投信（信託投資公司）連續或大量買超 → 主力建倉訊號
- 統一台股增長 (00981A) 新增/加碼成分股 → 主動型ETF操盤手看好
- 兩者同時發生 = 雙重確認，波段勝率最高
結果存入 market_signals (signal_type='smart_money')，供 app 顯示與 Telegram 推播
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from datetime import date
from loguru import logger
from sqlalchemy import text

from database.connection import get_session
from config.settings import tw_today

# ── 可調參數 ──────────────────────────────────────────────────────
# 注意：institutional_trading.invest_net 單位為「股」，門檻以張(=1000股)表示，比較時 ×1000
TRACK_ETF = "00981A"   # 追蹤的主動型 ETF（統一台股增長）
MIN_BUY_DAYS   = 3     # 投信至少連買幾天（在 LOOKBACK_DAYS 內）
MIN_SINGLE_BUY = 500   # 單日大量門檻（張）—— 達到此值即使只買 1 天也入選
LOOKBACK_DAYS  = 15    # 投信買超回看自然日數
ETF_LOOKBACK   = 45    # ETF 換股回看天數（主動型可能任意日換股）
_SHARES_PER_LOT = 1000  # 1 張 = 1000 股


# ── 查詢函式 ──────────────────────────────────────────────────────
def _invest_buying(session) -> list[dict]:
    """
    回傳近 LOOKBACK_DAYS 日投信淨買超的股票。
    條件：買超天數 >= MIN_BUY_DAYS，或單日買超 >= MIN_SINGLE_BUY 張。
    """
    rows = session.execute(text("""
        WITH window_ AS (
            SELECT it.stock_id,
                   it.trade_date,
                   it.invest_net,
                   s.stock_name
            FROM institutional_trading it
            JOIN stocks s ON it.stock_id = s.stock_id
            WHERE it.trade_date >= CURRENT_DATE - :days * INTERVAL '1 day'
        )
        SELECT
            stock_id,
            MAX(stock_name)                                                  AS stock_name,
            COUNT(*)  FILTER (WHERE invest_net > 0)                          AS buy_days,
            COUNT(*)  FILTER (WHERE invest_net < 0)                          AS sell_days,
            -- invest_net 單位為股，÷1000 轉成張
            ROUND(SUM(invest_net) FILTER (WHERE invest_net > 0) / 1000.0)    AS total_bought,
            ROUND(MAX(invest_net) / 1000.0)                                  AS peak_day,
            MAX(trade_date) FILTER (WHERE invest_net > 0)                    AS last_buy_date
        FROM window_
        GROUP BY stock_id
        HAVING
            COUNT(*) FILTER (WHERE invest_net > 0) >= :min_days
            OR MAX(invest_net) >= :min_single * :lot
        ORDER BY buy_days DESC, total_bought DESC NULLS LAST
        LIMIT 60
    """), {
        "days":       LOOKBACK_DAYS,
        "min_days":   MIN_BUY_DAYS,
        "min_single": MIN_SINGLE_BUY,
        "lot":        _SHARES_PER_LOT,
    }).fetchall()

    return [dict(r._mapping) for r in rows]


def _etf_additions(session, etf_id: str = TRACK_ETF) -> list[dict]:
    """回傳指定 ETF 近 ETF_LOOKBACK 日新增或加碼的成分股。"""
    rows = session.execute(text("""
        SELECT
            ec.stock_id,
            ec.stock_name,
            ec.change_type,
            ec.old_weight,
            ec.new_weight,
            ec.detected_date
        FROM etf_changes ec
        WHERE ec.etf_id    = :etf_id
          AND ec.change_type IN ('added', 'increased')
          AND ec.detected_date >= CURRENT_DATE - :days * INTERVAL '1 day'
        ORDER BY ec.detected_date DESC,
                 (COALESCE(ec.new_weight,0) - COALESCE(ec.old_weight,0)) DESC
        LIMIT 30
    """), {"etf_id": etf_id, "days": ETF_LOOKBACK}).fetchall()

    return [dict(r._mapping) for r in rows]


# ── 主入口 ────────────────────────────────────────────────────────
def run_smart_money_analysis() -> int:
    """
    計算聰明資金訊號並寫入 market_signals。
    已有今日訊號則跳過（idempotent）。
    回傳新增訊號數。
    """
    today = tw_today()
    logger.info("=== 聰明資金分析開始 ===")

    with get_session() as session:
        existing = session.execute(text("""
            SELECT COUNT(*) FROM market_signals
            WHERE signal_type = 'smart_money' AND signal_date = :dt
        """), {"dt": today}).scalar()

    if existing > 0:
        logger.info("  今日聰明資金訊號已存在，跳過")
        return 0

    with get_session() as session:
        invest_list = _invest_buying(session)
        etf_list    = _etf_additions(session)

    if not invest_list and not etf_list:
        logger.info("  今日無聰明資金訊號（institutional_trading / etf_changes 資料可能尚未更新）")
        return 0

    etf_ids     = {r["stock_id"] for r in etf_list}
    invest_ids  = {r["stock_id"] for r in invest_list}
    overlap_ids = etf_ids & invest_ids

    logger.info(f"  投信買超: {len(invest_list)} 支 | ETF 加碼: {len(etf_list)} 支 | 重疊: {len(overlap_ids)} 支")

    rows_to_save = []

    # ── 雙重確認（投信 + 統一ETF）──────────────────────────────
    for inv in invest_list:
        if inv["stock_id"] not in overlap_ids:
            continue
        etf = next(e for e in etf_list if e["stock_id"] == inv["stock_id"])
        action = "新納入" if etf["change_type"] == "added" else "加碼"
        weight_str = (f"{etf['old_weight'] or 0:.2f}% → {etf['new_weight'] or 0:.2f}%"
                      if etf.get("new_weight") else "")
        rows_to_save.append({
            "source":  "雙重確認",
            "title":   (f"⭐【雙確認】{inv['stock_id']} {inv['stock_name']} "
                        f"— 投信連買{inv['buy_days']}日 + 統一ETF{action}"),
            "summary": (f"投信近{LOOKBACK_DAYS}日買超 {inv['buy_days']} 天，"
                        f"累計 {(inv['total_bought'] or 0):,.0f} 張（峰值 {(inv['peak_day'] or 0):,.0f} 張）；"
                        f"統一台股增長({TRACK_ETF}) {action} {etf['detected_date']}"
                        + (f"，權重 {weight_str}" if weight_str else "")),
            "stocks":  [inv["stock_id"]],
        })

    # ── 純投信買超 ────────────────────────────────────────────
    for inv in invest_list:
        if inv["stock_id"] in overlap_ids:
            continue
        rows_to_save.append({
            "source":  "投信買超",
            "title":   (f"【投信】{inv['stock_id']} {inv['stock_name']} "
                        f"— 近{LOOKBACK_DAYS}日買超 {inv['buy_days']} 天"),
            "summary": (f"累計買超 {(inv['total_bought'] or 0):,.0f} 張，"
                        f"單日峰值 {(inv['peak_day'] or 0):,.0f} 張，"
                        f"最後買超日 {inv['last_buy_date']}"),
            "stocks":  [inv["stock_id"]],
        })

    # ── 純統一ETF換股 ─────────────────────────────────────────
    for etf in etf_list:
        if etf["stock_id"] in invest_ids:
            continue
        action = "新納入成分股" if etf["change_type"] == "added" else "加碼增持"
        weight_str = (f"{etf['old_weight'] or 0:.2f}% → {etf['new_weight'] or 0:.2f}%"
                      if etf.get("new_weight") else "")
        rows_to_save.append({
            "source":  "統一ETF",
            "title":   (f"【統一ETF】{etf['stock_id']} {etf['stock_name']} "
                        f"— {action}（{etf['detected_date']}）"),
            "summary": (f"統一台股增長({TRACK_ETF}) {action}"
                        + (f"，權重 {weight_str}" if weight_str else "")),
            "stocks":  [etf["stock_id"]],
        })

    saved = 0
    with get_session() as session:
        for r in rows_to_save:
            session.execute(text("""
                INSERT INTO market_signals
                    (signal_type, source, title, summary,
                     related_stocks, sentiment, signal_date)
                VALUES
                    ('smart_money', :source, :title, :summary,
                     :stocks, 'positive', :dt)
                ON CONFLICT DO NOTHING
            """), {**r, "dt": today})
            saved += 1

    logger.info(f"=== 聰明資金分析完成：新增 {saved} 個訊號 ===")
    return saved
