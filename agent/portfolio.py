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
from agent.strategy import decide_exit, STRATEGY


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
    """AI 部位（source='ai'）——排除手動倉！手動倉只建議不自動平倉，
    若不過濾，evaluate_exits 會把使用者的真實持股自動平倉。"""
    with get_session() as s:
        rows = s.execute(text("""
            SELECT p.stock_id, st.stock_name, p.entry_date, p.entry_price, p.peak_price
            FROM positions p JOIN stocks st ON st.stock_id = p.stock_id
            WHERE p.status = 'open' AND COALESCE(p.source, 'ai') = 'ai'
            ORDER BY p.entry_date
        """)).fetchall()
    return [dict(stock_id=r[0], stock_name=r[1], entry_date=r[2],
                 entry_price=float(r[3]), peak_price=float(r[4]) if r[4] else float(r[3]))
            for r in rows]


def _recent_rows(session, stock_id: str, on_date: date, n: int = 35) -> list[dict]:
    """取某股票 on_date(含)以前最近 n 筆完整 OHLCV + 技術指標，oldest→newest。"""
    rows = session.execute(text("""
        SELECT p.trade_date, p.open, p.high, p.low, p.close, p.volume,
               t.ma5, t.ma20, t.macd_hist, t.k_value, t.d_value
        FROM daily_prices p
        LEFT JOIN technical_indicators t
            ON t.stock_id = p.stock_id AND t.trade_date = p.trade_date
        WHERE p.stock_id = :sid AND p.trade_date <= :d AND p.close > 0
        ORDER BY p.trade_date DESC LIMIT :n
    """), {"sid": stock_id, "d": on_date, "n": n}).fetchall()

    return [
        dict(
            trade_date=r[0],
            open=float(r[1] or 0),
            high=float(r[2] or 0),
            low=float(r[3] or 0),
            close=float(r[4] or 0),
            volume=float(r[5] or 0),
            ma5=float(r[6]) if r[6] is not None else None,
            ma20=float(r[7]) if r[7] is not None else None,
            macd_hist=float(r[8]) if r[8] is not None else None,
            k_value=float(r[9]) if r[9] is not None else None,
            d_value=float(r[10]) if r[10] is not None else None,
        )
        for r in reversed(rows)  # oldest first
    ]


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

    # 風控守門員：同時持倉上限（參考 TradingAgents 的 Risk Manager 概念）
    max_open = STRATEGY.get("max_open_positions", 10)
    slots = max_open - len(held)
    if slots <= 0:
        logger.warning(f"持倉已達上限 {max_open} 檔，今日不開新倉")
        return []

    from agent.strategy import suggest_shares
    opened = []
    with get_session() as s:
        for pk in picks:
            if len(opened) >= slots:
                logger.info(f"已達持倉上限 {max_open} 檔，其餘推薦略過")
                break
            sid = pk["stock_id"]
            if sid in held:
                continue
            rows = _recent_rows(s, sid, on_date, n=1)
            if not rows or rows[-1]["close"] <= 0:
                continue
            price = rows[-1]["close"]
            # migration 07 後唯一約束為 partial index（WHERE source='ai'），
            # ON CONFLICT 需帶相同條件才能匹配
            s.execute(text("""
                INSERT INTO positions (stock_id, entry_date, entry_price, entry_reason, peak_price)
                VALUES (:sid, :d, :price, :reason, :price)
                ON CONFLICT (stock_id, entry_date) WHERE source = 'ai' DO NOTHING
            """), {"sid": sid, "d": on_date, "price": price, "reason": pk.get("reason", "")})
            opened.append({"stock_id": sid, "entry_price": price,
                           "shares": suggest_shares(price)})
    if opened:
        logger.info(f"📌 新增追蹤部位 {len(opened)} 檔")
    return opened


def exit_cfg() -> dict:
    """
    出場參數：空頭時（市場濾網觸發）加回死亡交叉保護，多頭時用預設。
    portfolio / manual_advisor 共用，確保 AI 部位與手動倉判斷一致。
    """
    if STRATEGY.get("bear_reenable_death_cross"):
        try:
            from agent.stock_selector import market_is_bull
            if not market_is_bull():
                return {**STRATEGY, "exit_on_death_cross": True}
        except Exception as e:
            logger.warning(f"市場濾網檢查失敗（用預設出場參數）: {e}")
    return STRATEGY


def evaluate_exits(on_date: date) -> list[dict]:
    """
    檢查所有持有中部位：更新最高價，依 strategy.decide_exit 判斷是否出場。
    回傳今日「該出場」的清單（含原因與報酬）。
    """
    ensure_positions_table()
    cfg = exit_cfg()
    avg_days = cfg.get("volume_avg_days", 20)
    exits = []

    with get_session() as s:
        for pos in open_positions():
            sid = pos["stock_id"]
            history = _recent_rows(s, sid, on_date, n=max(40, avg_days + 5))
            if not history:
                continue
            today = history[-1]
            close = today["close"]
            if close <= 0:
                continue

            ma5  = today["ma5"]
            ma20 = today["ma20"]

            # 計算當日均量（avg_days 根，不含今天）
            past = history[-(avg_days + 1):-1]
            avg_vol = (sum(r["volume"] for r in past) / len(past)) if past else None

            # 準備 extra（當日技術細節）
            prev = history[-2] if len(history) >= 2 else {}
            extra = dict(
                k=today["k_value"],
                d=today["d_value"],
                k_prev=prev.get("k_value"),
                d_prev=prev.get("d_value"),
                macd_hist=today["macd_hist"],
                macd_hist_prev=prev.get("macd_hist"),
                open=today["open"],
                high=today["high"],
                low=today["low"],
                volume=today["volume"],
                avg_volume=avg_vol,
            )

            peak = max(pos["peak_price"], close)
            s.execute(text("UPDATE positions SET peak_price=:pk WHERE stock_id=:sid AND status='open'"),
                      {"pk": peak, "sid": sid})

            hold = _holding_days(s, sid, pos["entry_date"], on_date)
            should_exit, reason = decide_exit(
                pos["entry_price"], peak, close,
                ma5, ma20, hold,
                cfg=cfg,
                extra=extra,
                history=history,
            )
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

    if opened:
        from agent.strategy import format_size
        cfg = STRATEGY
        lines.append(f"\n💰 部位規模建議（資金 {cfg['capital']:,} 元、單筆風險 {cfg['risk_per_trade']*100:.0f}%）：")
        for o in opened:
            lines.append(f"  {o['stock_id']} @ {o['entry_price']:.2f} → "
                         f"{format_size(o.get('shares', 0))}")

    held = open_positions()
    lines.append(f"📦 目前追蹤中部位：{len(held)} 檔（上限 {STRATEGY.get('max_open_positions', 10)}）")
    return "\n".join(lines)
