"""
agent/portfolio.py

部位追蹤：agent 記住推薦(買進)過的股票，每天用 strategy 的出場規則檢查，
到適當時機就發出「出場」提醒。把「選股」與「抱單/賣出」分開管理。

主要函式：
  - ensure_positions_table(): 確保 positions 表存在（現有 DB 也能用）
  - open_positions(): 目前持有中的部位
  - record_entries(picks, date): 把今日推薦開成新部位（已持有則略過）
  - evaluate_exits(date): 檢查所有持有部位是否該出場，更新最高價/平倉
"""
from datetime import date
from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from database.connection import get_session
from agent.strategy import decide_exit


def ensure_positions_table():
    """現有 DB（init.sql 不會重跑）也能自動建立 positions 表。"""
    with get_session() as s:
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS positions (
                id           BIGSERIAL    PRIMARY KEY,
                stock_id     VARCHAR(10)  NOT NULL REFERENCES stocks(stock_id),
                entry_date   DATE         NOT NULL,
                entry_price  NUMERIC(12,2) NOT NULL,
                entry_reason TEXT,
                peak_price   NUMERIC(12,2),
                status       VARCHAR(10)  NOT NULL DEFAULT 'open'
                                 CHECK (status IN ('open','closed')),
                exit_date    DATE,
                exit_price   NUMERIC(12,2),
                exit_reason  TEXT,
                return_pct   NUMERIC(8,4),
                created_at   TIMESTAMPTZ  DEFAULT NOW(),
                UNIQUE (stock_id, entry_date)
            )
        """))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status)"))


def open_positions() -> list[dict]:
    with get_session() as s:
        rows = s.execute(text("""
            SELECT p.stock_id, st.stock_name, p.entry_date, p.entry_price, p.peak_price
            FROM positions p JOIN stocks st ON st.stock_id = p.stock_id
            WHERE p.status = 'open' ORDER BY p.entry_date
        """)).fetchall()
    return [dict(stock_id=r[0], stock_name=r[1], entry_date=r[2],
                 entry_price=float(r[3]), peak_price=float(r[4]) if r[4] else float(r[3]))
            for r in rows]


def _latest_row(session, stock_id, on_date: date):
    """取某股票在 on_date(含)以前最近一筆 收盤 + MA5/MA20。"""
    return session.execute(text("""
        SELECT p.trade_date, p.close, t.ma5, t.ma20
        FROM daily_prices p
        LEFT JOIN technical_indicators t
            ON t.stock_id = p.stock_id AND t.trade_date = p.trade_date
        WHERE p.stock_id = :sid AND p.trade_date <= :d
        ORDER BY p.trade_date DESC LIMIT 1
    """), {"sid": stock_id, "d": on_date}).fetchone()


def _holding_days(session, stock_id, entry_date, on_date) -> int:
    return session.execute(text("""
        SELECT COUNT(DISTINCT trade_date) FROM daily_prices
        WHERE stock_id = :sid AND trade_date > :e AND trade_date <= :d
    """), {"sid": stock_id, "e": entry_date, "d": on_date}).scalar() or 0


def record_entries(picks: list[dict], on_date: date) -> list[dict]:
    """
    picks: [{stock_id, reason}] —— 今日推薦清單。
    以 on_date(含)前最近收盤為進場價開新部位；若已持有同股則略過。
    回傳實際新開的部位清單。
    """
    ensure_positions_table()
    held = {p["stock_id"] for p in open_positions()}
    opened = []
    with get_session() as s:
        for pk in picks:
            sid = pk["stock_id"]
            if sid in held:
                continue
            row = _latest_row(s, sid, on_date)
            if not row or row[1] is None:
                continue
            price = float(row[1])
            s.execute(text("""
                INSERT INTO positions (stock_id, entry_date, entry_price, entry_reason, peak_price)
                VALUES (:sid, :d, :price, :reason, :price)
                ON CONFLICT (stock_id, entry_date) DO NOTHING
            """), {"sid": sid, "d": on_date, "price": price, "reason": pk.get("reason", "")})
            opened.append({"stock_id": sid, "entry_price": price})
    if opened:
        logger.info(f"📌 新增追蹤部位 {len(opened)} 檔")
    return opened


def evaluate_exits(on_date: date) -> list[dict]:
    """
    檢查所有持有中部位：更新最高價，依 strategy.decide_exit 判斷是否出場。
    回傳今日「該出場」的清單（含原因與報酬）。
    """
    ensure_positions_table()
    exits = []
    with get_session() as s:
        for pos in open_positions():
            sid = pos["stock_id"]
            row = _latest_row(s, sid, on_date)
            if not row or row[1] is None or float(row[1]) <= 0:
                continue   # 無資料或 close<=0（停牌/瑕疵）→ 當日不判斷，續抱
            _, close, ma5, ma20 = row
            close = float(close)
            ma5 = float(ma5) if ma5 is not None else None
            ma20 = float(ma20) if ma20 is not None else None

            peak = max(pos["peak_price"], close)
            s.execute(text("UPDATE positions SET peak_price=:pk WHERE stock_id=:sid AND status='open'"),
                      {"pk": peak, "sid": sid})

            hold = _holding_days(s, sid, pos["entry_date"], on_date)
            should_exit, reason = decide_exit(pos["entry_price"], peak, close, ma5, ma20, hold)
            if should_exit:
                ret = (close / pos["entry_price"] - 1) * 100
                s.execute(text("""
                    UPDATE positions SET status='closed', exit_date=:d, exit_price=:px,
                        exit_reason=:r, return_pct=:ret
                    WHERE stock_id=:sid AND status='open'
                """), {"d": on_date, "px": close, "r": reason, "ret": round(ret, 4), "sid": sid})
                exits.append({"stock_id": sid, "stock_name": pos["stock_name"],
                              "entry_price": pos["entry_price"], "exit_price": close,
                              "return_pct": ret, "reason": reason, "holding_days": hold})
    if exits:
        logger.info(f"🔔 觸發出場 {len(exits)} 檔")
    return exits


def format_positions_report(exits: list[dict], opened: list[dict]) -> str:
    """組出場/進場提醒文字（給 Telegram / 終端）。"""
    lines = []
    if exits:
        lines.append("🔔 今日出場提醒：")
        for e in exits:
            sign = "+" if e["return_pct"] >= 0 else ""
            lines.append(f"  賣出 {e['stock_id']} {e['stock_name']}："
                         f"{sign}{e['return_pct']:.1f}%（持有{e['holding_days']}日，{e['reason']}）")
    else:
        lines.append("🔔 今日無出場訊號（續抱）")
    held = open_positions()
    lines.append(f"📦 目前追蹤中部位：{len(held)} 檔")
    return "\n".join(lines)
