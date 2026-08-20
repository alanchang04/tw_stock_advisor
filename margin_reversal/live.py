"""Independent daily signal/order/position lifecycle."""
from __future__ import annotations

import json
from datetime import date, timedelta

from sqlalchemy import text

from database.connection import get_session
from .config import MARGIN_REVERSAL_CONFIG
from .data import load_research_frame
from .signals import build_feature_panel, select_signals


def persist_daily_signals(on_date: date, cfg: dict | None = None) -> int:
    cfg = {**MARGIN_REVERSAL_CONFIG, **(cfg or {})}
    if not cfg.get("enabled", False):
        return 0
    frame, market = load_research_frame(on_date - timedelta(days=120), on_date)
    if frame.empty:
        return 0
    chosen = select_signals(build_feature_panel(frame, market), cfg["max_positions"])
    chosen = chosen[chosen["trade_date"].dt.date.eq(on_date)]
    count = 0
    with get_session() as s:
        for r in chosen.to_dict("records"):
            features = {k: r.get(k) for k in (
                "margin_drop_5d", "price_drawdown_20d", "revenue_yoy",
                "inst_buy_streak", "market_drawdown_20d")}
            signal_id = s.execute(text("""
                INSERT INTO margin_reversal_signals
                    (stock_id, signal_date, score, market_stress, margin_wash,
                     price_oversold, fundamental_ok, institutional_ok, features)
                VALUES (:sid,:d,:score,:ms,:mw,:po,:fo,:io,CAST(:features AS JSONB))
                ON CONFLICT(stock_id, signal_date) DO UPDATE SET
                    score=EXCLUDED.score, features=EXCLUDED.features
                RETURNING id
            """), {"sid": str(r["stock_id"]), "d": on_date, "score": float(r["score"]),
                    "ms": bool(r["market_stress"]), "mw": bool(r["margin_wash"]),
                    "po": bool(r["price_oversold"]), "fo": bool(r["fundamental_ok"]),
                    "io": bool(r["institutional_ok"]),
                    "features": json.dumps(features, default=float)}).scalar()
            s.execute(text("""
                INSERT INTO margin_reversal_orders(side, stock_id, signal_id, signal_date, reason)
                VALUES ('buy',:sid,:signal_id,:d,'qualified margin wash reversal')
                ON CONFLICT(stock_id, side) WHERE status='pending' DO NOTHING
            """), {"sid": str(r["stock_id"]), "signal_id": signal_id, "d": on_date})
            count += 1
    return count


def _ensure_account(session, cfg):
    return session.execute(text("""
        INSERT INTO margin_reversal_accounts(strategy_key, initial_capital, cash)
        VALUES ('margin_reversal',:capital,:capital)
        ON CONFLICT(strategy_key) DO UPDATE SET strategy_key=EXCLUDED.strategy_key
        RETURNING id, cash
    """), {"capital": cfg["capital"]}).fetchone()


def fill_pending_orders(on_date: date, cfg: dict | None = None) -> dict:
    cfg = {**MARGIN_REVERSAL_CONFIG, **(cfg or {})}
    fee, tax, slip = cfg["fee_rate"], cfg["tax_rate"], cfg["slippage"]
    filled = {"buys": 0, "sells": 0}
    with get_session() as s:
        aid, cash = _ensure_account(s, cfg); cash = float(cash)
        orders = s.execute(text("""
            SELECT id, side, stock_id, signal_id, position_id, reason
            FROM margin_reversal_orders WHERE status='pending' AND signal_date < :d
            ORDER BY side DESC, id FOR UPDATE
        """), {"d": on_date}).fetchall()
        for oid, side, sid, signal_id, pid, reason in orders:
            op = s.execute(text("""
                SELECT open FROM daily_prices WHERE stock_id=:sid AND trade_date=:d AND open>0
            """), {"sid": sid, "d": on_date}).scalar()
            if op is None:
                continue
            if side == "sell":
                pos = s.execute(text("""
                    SELECT shares, entry_cost FROM margin_reversal_positions
                    WHERE id=:pid AND status='open' FOR UPDATE
                """), {"pid": pid}).fetchone()
                if not pos:
                    s.execute(text("UPDATE margin_reversal_orders SET status='cancelled' WHERE id=:id"), {"id": oid}); continue
                fill = float(op)*(1-slip); proceeds = int(pos[0])*fill*(1-fee-tax)
                cash += proceeds
                s.execute(text("""
                    UPDATE margin_reversal_positions SET status='closed', exit_date=:d,
                        exit_price=:px, exit_reason=:reason, exit_proceeds=:proceeds,
                        net_pnl=:pnl WHERE id=:pid
                """), {"d": on_date, "px": round(fill,2), "reason": reason,
                        "proceeds": round(proceeds,2), "pnl": round(proceeds-float(pos[1]),2), "pid": pid})
                filled["sells"] += 1
            else:
                open_count = s.execute(text("SELECT COUNT(*) FROM margin_reversal_positions WHERE status='open'" )).scalar()
                if open_count >= cfg["max_positions"]:
                    continue
                fill = float(op)*(1+slip)
                nav_value = s.execute(text("""
                    SELECT COALESCE(SUM(p.shares*COALESCE(d.close,p.entry_price)),0)
                    FROM margin_reversal_positions p LEFT JOIN daily_prices d
                      ON d.stock_id=p.stock_id AND d.trade_date=:d WHERE p.status='open'
                """), {"d": on_date}).scalar() or 0
                budget = min(cash, (cash+float(nav_value))*cfg["position_fraction"])
                shares = int(budget//(fill*(1+fee)))
                if shares <= 0:
                    continue
                cost = shares*fill*(1+fee); cash -= cost
                s.execute(text("""
                    INSERT INTO margin_reversal_positions
                        (signal_id,stock_id,entry_date,entry_price,shares,entry_cost)
                    VALUES (:signal_id,:sid,:d,:px,:shares,:cost)
                """), {"signal_id": signal_id, "sid": sid, "d": on_date,
                        "px": round(fill,2), "shares": shares, "cost": round(cost,2)})
                filled["buys"] += 1
            s.execute(text("""
                UPDATE margin_reversal_orders SET status='filled',fill_date=:d,fill_price=:px WHERE id=:id
            """), {"d": on_date, "px": round(fill,2), "id": oid})
        s.execute(text("""
            UPDATE margin_reversal_accounts SET cash=:cash,last_nav=:cash+(
                SELECT COALESCE(SUM(p.shares*COALESCE(d.close,p.entry_price)),0)
                FROM margin_reversal_positions p LEFT JOIN daily_prices d
                  ON d.stock_id=p.stock_id AND d.trade_date=:d WHERE p.status='open'),updated_at=NOW()
            WHERE id=:aid
        """), {"cash": round(cash,2), "d": on_date, "aid": aid})
    return filled


def queue_exits(on_date: date, cfg: dict | None = None) -> int:
    cfg = {**MARGIN_REVERSAL_CONFIG, **(cfg or {})}; queued = 0
    with get_session() as s:
        rows = s.execute(text("""
            SELECT p.id,p.stock_id,p.entry_date,p.entry_price,d.close
            FROM margin_reversal_positions p JOIN daily_prices d ON d.stock_id=p.stock_id
            WHERE p.status='open' AND d.trade_date=:d
        """), {"d": on_date}).fetchall()
        for pid,sid,entry_date,entry_price,close in rows:
            ret=float(close)/float(entry_price)-1
            held=s.execute(text("SELECT COUNT(*) FROM daily_prices WHERE stock_id=:sid AND trade_date>:e AND trade_date<=:d"),
                           {"sid":sid,"e":entry_date,"d":on_date}).scalar()
            reason=("stop_loss" if ret<=-cfg["stop_loss"] else "take_profit" if ret>=cfg["take_profit"] else
                    "max_hold" if held>=cfg["max_hold_days"] else None)
            if reason:
                s.execute(text("""
                    INSERT INTO margin_reversal_orders(side,stock_id,position_id,signal_date,reason)
                    VALUES('sell',:sid,:pid,:d,:reason)
                    ON CONFLICT(stock_id,side) WHERE status='pending' DO NOTHING
                """), {"sid":sid,"pid":pid,"d":on_date,"reason":reason}); queued+=1
    return queued
