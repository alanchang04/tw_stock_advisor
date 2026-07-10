"""
agent/portfolio.py

部位追蹤：agent 記住推薦(買進)過的股票，每天用 strategy 的出場規則檢查，
到適當時機就發出「出場」提醒。把「選股」與「抱單/賣出」分開管理。

【階段0b：掛單 → 隔日開盤成交】
pipeline 於收盤後 20:00~22:30 才跑，當日收盤價早已成交完畢、根本買不到。
故不再用當日收盤價直接建倉/平倉，改為兩階段：
  今晚（D 日收盤後）算訊號 → 寫進 pending_orders
  明晚（D+1 pipeline）用 D+1 的「開盤價 ± 滑價」真正成交
這同時是接券商 API 的架構前置（委託 → 成交回報）。

主要函式：
  - ensure_positions_table() / ensure_pending_orders_table(): 冪等建表
  - open_positions(): 目前持有中的 AI 部位
  - fill_pending_orders(date): 用今日開盤價成交昨日掛的單（pipeline 最先跑）
  - queue_entries(picks, date): 把今日推薦掛成「明日開盤買進」委託
  - queue_exits(date): 檢查持有部位，該走的掛成「明日開盤賣出」委託
"""
from datetime import date, timedelta
from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from database.connection import get_session
from agent.strategy import (decide_exit, STRATEGY, FEE_RATE, TAX_RATE, SLIPPAGE,
                            net_return, buy_fill, sell_fill)

# 買單超過這天數還沒成交（例如 pipeline 連續掛掉）就作廢，不追過期訊號。
# 需大於連假長度（春節可達 9 天），否則會誤殺正常的假期後成交。
BUY_ORDER_STALE_DAYS = 10


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


def ensure_pending_orders_table():
    """掛單簿（migration 14）。冪等，現有 DB 直接自建，不動既有資料。"""
    with get_session() as s:
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS pending_orders (
                id           BIGSERIAL     PRIMARY KEY,
                side         VARCHAR(4)    NOT NULL CHECK (side IN ('buy','sell')),
                stock_id     VARCHAR(10)   NOT NULL REFERENCES stocks(stock_id),
                signal_date  DATE          NOT NULL,
                signal_price NUMERIC(12,2),
                reason       TEXT,
                position_id  BIGINT        REFERENCES positions(id),
                status       VARCHAR(10)   NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending','filled','cancelled','expired')),
                fill_date    DATE,
                fill_price   NUMERIC(12,2),
                created_at   TIMESTAMPTZ   DEFAULT NOW()
            )
        """))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_pending_orders_status ON pending_orders (status)"))
        s.execute(text("""CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_orders_open
                          ON pending_orders (stock_id, side) WHERE status = 'pending'"""))
        # 不改寫 return_pct 既有語意（舊資料是舊模型的毛報酬），另存訊號價與淨報酬
        for col, typ in [("signal_price", "NUMERIC(12,2)"),
                         ("exit_signal_price", "NUMERIC(12,2)"),
                         ("net_return_pct", "NUMERIC(8,4)")]:
            s.execute(text(f"ALTER TABLE positions ADD COLUMN IF NOT EXISTS {col} {typ}"))


def _open_price(session, stock_id: str, on_date: date) -> float | None:
    """on_date 當日開盤價（無資料/停牌 → None）。"""
    r = session.execute(text("""
        SELECT open FROM daily_prices
        WHERE stock_id = :sid AND trade_date = :d AND open > 0
    """), {"sid": stock_id, "d": on_date}).fetchone()
    return float(r[0]) if r else None


def open_positions() -> list[dict]:
    """AI 部位（source='ai'）——排除手動倉！手動倉只建議不自動平倉，
    若不過濾，queue_exits 會把使用者的真實持股掛單自動平倉。"""
    with get_session() as s:
        rows = s.execute(text("""
            SELECT p.stock_id, st.stock_name, p.entry_date, p.entry_price, p.peak_price, p.id
            FROM positions p JOIN stocks st ON st.stock_id = p.stock_id
            WHERE p.status = 'open' AND COALESCE(p.source, 'ai') = 'ai'
            ORDER BY p.entry_date
        """)).fetchall()
    return [dict(stock_id=r[0], stock_name=r[1], entry_date=r[2],
                 entry_price=float(r[3]), peak_price=float(r[4]) if r[4] else float(r[3]),
                 id=r[5])
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


def _pending(session, side: str) -> set:
    rows = session.execute(text(
        "SELECT stock_id FROM pending_orders WHERE side = :s AND status = 'pending'"
    ), {"s": side}).fetchall()
    return {r[0] for r in rows}


def queue_entries(picks: list[dict], on_date: date) -> list[dict]:
    """
    picks: [{stock_id, reason}] —— 今日（on_date 收盤）算出的推薦。
    不直接建倉，而是掛成「隔日開盤買進」委託（見模組說明的階段0b）。
    回傳實際掛出的委託清單。
    """
    ensure_positions_table()
    ensure_pending_orders_table()
    held = {p["stock_id"] for p in open_positions()}

    # 風控守門員：同時持倉上限。已掛未成交的買單也要佔用名額，否則會超買。
    max_open = STRATEGY.get("max_open_positions", 10)
    with get_session() as s:
        pending_buys = _pending(s, "buy")
    slots = max_open - len(held) - len(pending_buys)
    if slots <= 0:
        logger.warning(f"持倉({len(held)})+未成交買單({len(pending_buys)}) 已達上限 {max_open}，今日不掛新買單")
        return []

    queued = []
    with get_session() as s:
        for pk in picks:
            if len(queued) >= slots:
                logger.info(f"已達持倉上限 {max_open} 檔，其餘推薦略過")
                break
            sid = pk["stock_id"]
            if sid in held or sid in pending_buys:
                continue
            rows = _recent_rows(s, sid, on_date, n=1)
            if not rows or rows[-1]["close"] <= 0:
                continue
            signal_price = rows[-1]["close"]
            s.execute(text("""
                INSERT INTO pending_orders (side, stock_id, signal_date, signal_price, reason)
                VALUES ('buy', :sid, :d, :px, :reason)
                ON CONFLICT DO NOTHING
            """), {"sid": sid, "d": on_date, "px": signal_price, "reason": pk.get("reason", "")})
            queued.append({"stock_id": sid, "signal_price": signal_price,
                           "reason": pk.get("reason", "")})
    if queued:
        logger.info(f"📌 掛出隔日開盤買單 {len(queued)} 檔")
    return queued


def fill_pending_orders(on_date: date) -> dict:
    """
    用 on_date 的「開盤價 ± 滑價」成交先前掛的委託（pipeline 最先呼叫）。
      - 只成交 signal_date < on_date 的單（今晚剛掛的要等明天）
      - 賣單：無開盤價（停牌）→ 順延；買單：無開盤價 → 取消（不追價）
      - 買單逾 BUY_ORDER_STALE_DAYS 天未成交 → 作廢（過期訊號不追）
    回傳 {"entries": [...], "exits": [...]}（實際成交的）。
    """
    ensure_positions_table()
    ensure_pending_orders_table()
    from agent.strategy import suggest_shares

    filled_entries, filled_exits = [], []
    with get_session() as s:
        orders = s.execute(text("""
            SELECT o.id, o.side, o.stock_id, o.signal_date, o.signal_price, o.reason,
                   o.position_id, st.stock_name
            FROM pending_orders o JOIN stocks st ON st.stock_id = o.stock_id
            WHERE o.status = 'pending' AND o.signal_date < :d
            ORDER BY o.side DESC, o.id      -- 先賣後買：釋出名額與資金
        """), {"d": on_date}).fetchall()

        held = {p["stock_id"] for p in open_positions()}
        max_open = STRATEGY.get("max_open_positions", 10)

        for oid, side, sid, sig_date, sig_px, reason, pos_id, name in orders:
            op = _open_price(s, sid, on_date)

            if side == "sell":
                if op is None:            # 停牌：賣單順延，繼續 pending
                    logger.warning(f"  {sid} {name} 無開盤價，賣單順延")
                    continue
                fill = sell_fill(op)
                pos = s.execute(text("""
                    SELECT id, entry_price, entry_date FROM positions
                    WHERE id = :pid AND status = 'open'
                """), {"pid": pos_id}).fetchone() if pos_id else None
                if pos is None:           # 部位已不在（例如手動平倉過）→ 作廢此單
                    s.execute(text("UPDATE pending_orders SET status='cancelled' WHERE id=:i"), {"i": oid})
                    continue
                entry_px = float(pos[1])
                gross = (fill / entry_px - 1) * 100
                net   = net_return(entry_px, fill) * 100
                hold  = _holding_days(s, sid, pos[2], on_date)
                s.execute(text("""
                    UPDATE positions SET status='closed', exit_date=:d, exit_price=:px,
                        exit_reason=:r, return_pct=:g, net_return_pct=:n, exit_signal_price=:sp
                    WHERE id = :pid
                """), {"d": on_date, "px": round(fill, 2), "r": reason, "g": round(gross, 4),
                       "n": round(net, 4), "sp": sig_px, "pid": pos[0]})
                s.execute(text("""
                    UPDATE pending_orders SET status='filled', fill_date=:d, fill_price=:px
                    WHERE id = :i
                """), {"d": on_date, "px": round(fill, 2), "i": oid})
                held.discard(sid)
                filled_exits.append({"stock_id": sid, "stock_name": name,
                                     "entry_price": entry_px, "exit_price": fill,
                                     "return_pct": gross, "net_return_pct": net,
                                     "reason": reason, "holding_days": hold})
                continue

            # ── buy ──
            if (on_date - sig_date).days > BUY_ORDER_STALE_DAYS:
                s.execute(text("UPDATE pending_orders SET status='expired' WHERE id=:i"), {"i": oid})
                logger.warning(f"  {sid} {name} 買單過期作廢（訊號 {sig_date}）")
                continue
            if op is None:                # 停牌：買單不追價，直接取消
                s.execute(text("UPDATE pending_orders SET status='cancelled' WHERE id=:i"), {"i": oid})
                continue
            if sid in held or len(held) >= max_open:
                s.execute(text("UPDATE pending_orders SET status='cancelled' WHERE id=:i"), {"i": oid})
                continue
            fill = buy_fill(op)
            s.execute(text("""
                INSERT INTO positions (stock_id, entry_date, entry_price, entry_reason,
                                       peak_price, signal_price)
                VALUES (:sid, :d, :px, :reason, :px, :sp)
                ON CONFLICT (stock_id, entry_date) WHERE source = 'ai' DO NOTHING
            """), {"sid": sid, "d": on_date, "px": round(fill, 2),
                   "reason": reason, "sp": sig_px})
            s.execute(text("""
                UPDATE pending_orders SET status='filled', fill_date=:d, fill_price=:px
                WHERE id = :i
            """), {"d": on_date, "px": round(fill, 2), "i": oid})
            held.add(sid)
            filled_entries.append({"stock_id": sid, "stock_name": name, "entry_price": fill,
                                   "signal_price": float(sig_px) if sig_px else None,
                                   "shares": suggest_shares(fill), "reason": reason})

    if filled_entries or filled_exits:
        logger.info(f"✅ 開盤成交：買進 {len(filled_entries)} 檔、賣出 {len(filled_exits)} 檔")
    return {"entries": filled_entries, "exits": filled_exits}


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


def queue_exits(on_date: date) -> list[dict]:
    """
    檢查所有持有中的 AI 部位：更新最高價，依 strategy.decide_exit 判斷是否該出場。
    **不直接平倉**，而是掛成「隔日開盤賣出」委託（階段0b）。
    回傳今日新掛出的賣單清單（含原因與當下未實現損益，供 Telegram 提醒）。
    """
    ensure_positions_table()
    ensure_pending_orders_table()
    cfg = exit_cfg()
    avg_days = cfg.get("volume_avg_days", 20)
    exits = []

    with get_session() as s:
        pending_sells = _pending(s, "sell")
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
            # 用 id 限定：同一檔股票可能同時有手動倉，不可誤更新到別人的部位
            s.execute(text("UPDATE positions SET peak_price=:pk WHERE id=:pid"),
                      {"pk": peak, "pid": pos["id"]})

            hold = _holding_days(s, sid, pos["entry_date"], on_date)
            should_exit, reason = decide_exit(
                pos["entry_price"], peak, close,
                ma5, ma20, hold,
                cfg=cfg,
                extra=extra,
                history=history,
            )
            if should_exit and sid not in pending_sells:
                # 掛「明日開盤賣出」；今日收盤價只當訊號價，不是成交價
                s.execute(text("""
                    INSERT INTO pending_orders
                        (side, stock_id, signal_date, signal_price, reason, position_id)
                    VALUES ('sell', :sid, :d, :px, :r, :pid)
                    ON CONFLICT DO NOTHING
                """), {"sid": sid, "d": on_date, "px": close, "r": reason, "pid": pos["id"]})
                unreal = (close / pos["entry_price"] - 1) * 100
                exits.append({"stock_id": sid, "stock_name": pos["stock_name"],
                              "entry_price": pos["entry_price"], "signal_price": close,
                              "return_pct": unreal, "reason": reason, "holding_days": hold})
    if exits:
        logger.info(f"🔔 掛出隔日開盤賣單 {len(exits)} 檔")
    return exits


def format_positions_report(queued_exits: list[dict], queued_entries: list[dict],
                            filled: dict | None = None) -> str:
    """
    組報告文字（給 Telegram / 終端）。階段0b 後分成兩塊：
      1) 今日開盤「已成交」的（昨天掛的單）
      2) 明日開盤「要成交」的新委託（今晚算出的訊號）
    """
    from agent.strategy import format_size
    lines = []
    filled = filled or {}

    fe, fx = filled.get("entries", []), filled.get("exits", [])
    if fe or fx:
        lines.append("✅ 今日開盤已成交（昨日掛單）：")
        for e in fx:
            sign = "+" if e["net_return_pct"] >= 0 else ""
            lines.append(f"  賣出 {e['stock_id']} {e['stock_name']} @ {e['exit_price']:.2f}"
                         f"　淨{sign}{e['net_return_pct']:.1f}%"
                         f"（持有{e['holding_days']}日，{e['reason']}）")
        for e in fe:
            slip = ""
            if e.get("signal_price"):
                gap = (e["entry_price"] / e["signal_price"] - 1) * 100
                slip = f"（訊號價 {e['signal_price']:.2f}，開盤價差 {gap:+.1f}%）"
            lines.append(f"  買進 {e['stock_id']} {e['stock_name']} @ {e['entry_price']:.2f}"
                         f" → {format_size(e.get('shares', 0))}{slip}")
        lines.append("")

    if queued_exits:
        lines.append("🔔 明日開盤賣出（今日觸發出場）：")
        for e in queued_exits:
            sign = "+" if e["return_pct"] >= 0 else ""
            lines.append(f"  賣出 {e['stock_id']} {e['stock_name']}："
                         f"目前{sign}{e['return_pct']:.1f}%（持有{e['holding_days']}日，{e['reason']}）")
    else:
        lines.append("🔔 今日無出場訊號（續抱）")

    if queued_entries:
        cfg = STRATEGY
        lines.append(f"\n💰 明日開盤買進（資金 {cfg['capital']:,} 元、單筆風險 {cfg['risk_per_trade']*100:.0f}%）：")
        for o in queued_entries:
            lines.append(f"  {o['stock_id']}（訊號價 {o['signal_price']:.2f}，"
                         f"實際以明日開盤價成交）")

    held = open_positions()
    lines.append(f"📦 目前持有部位：{len(held)} 檔（上限 {STRATEGY.get('max_open_positions', 10)}）")
    return "\n".join(lines)
