"""Account-level cash and NAV helpers for the swing-strategy paper ledger."""
from __future__ import annotations

import math
from datetime import date

from sqlalchemy import text

from agent.strategy import FEE_RATE, STRATEGY
from database.connection import get_session

SWING_ACCOUNT_KEY = "swing"


def live_entry_share_count(price: float, cash: float, nav: float, max_open: int,
                           risk_shares: int, avg_volume: float | None,
                           max_pct_of_avg_volume: float,
                           fee_rate: float = FEE_RATE) -> int:
    """Apply cash, equal-NAV-slot, risk and liquidity caps to an actual fill."""
    if price <= 0 or cash <= 0 or nav <= 0 or max_open <= 0 or risk_shares <= 0:
        return 0
    affordable = int(min(cash, nav / max_open) // (price * (1 + fee_rate)))
    liquid = (int(avg_volume * max_pct_of_avg_volume)
              if avg_volume is not None and math.isfinite(avg_volume) and avg_volume > 0
              else affordable)
    return max(0, min(int(risk_shares), affordable, liquid))


def ensure_paper_account(cfg: dict = STRATEGY) -> None:
    """Create the forward paper account without inventing quantities for legacy rows."""
    capital = float(cfg["capital"])
    with get_session() as s:
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS paper_accounts (
                id BIGSERIAL PRIMARY KEY, strategy_key VARCHAR(40) NOT NULL UNIQUE,
                initial_capital NUMERIC(18,2) NOT NULL CHECK (initial_capital > 0),
                cash NUMERIC(18,2) NOT NULL CHECK (cash >= 0), last_nav NUMERIC(18,2),
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())
        """))
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS paper_account_transactions (
                id BIGSERIAL PRIMARY KEY, account_id BIGINT NOT NULL REFERENCES paper_accounts(id),
                position_id BIGINT REFERENCES positions(id), trade_date DATE NOT NULL,
                kind VARCHAR(20) NOT NULL CHECK (kind IN ('deposit','buy','sell','adjustment')),
                amount NUMERIC(18,2) NOT NULL, cash_after NUMERIC(18,2) NOT NULL CHECK (cash_after >= 0),
                note TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())
        """))
        s.execute(text("ALTER TABLE positions ADD COLUMN IF NOT EXISTS paper_account_id BIGINT REFERENCES paper_accounts(id)"))
        known_open_cost = float(s.execute(text("""
            SELECT COALESCE(SUM(entry_cost), 0) FROM positions
            WHERE status='open' AND COALESCE(source,'ai')='ai' AND shares IS NOT NULL
        """)).scalar() or 0)
        opening_cash = max(0.0, capital - known_open_cost)
        row = s.execute(text("""
            INSERT INTO paper_accounts (strategy_key, initial_capital, cash)
            VALUES (:key, :capital, :cash)
            ON CONFLICT (strategy_key) DO NOTHING RETURNING id
        """), {"key": SWING_ACCOUNT_KEY, "capital": capital, "cash": opening_cash}).fetchone()
        if row:
            # Known quantities are not invented: a brand-new ledger may adopt them and
            # opening_cash already subtracts their recorded entry_cost above.
            s.execute(text("""
                UPDATE positions SET paper_account_id=:aid
                WHERE status='open' AND COALESCE(source,'ai')='ai'
                  AND shares IS NOT NULL AND entry_cost IS NOT NULL
                  AND paper_account_id IS NULL
            """), {"aid": row[0]})
            s.execute(text("""
                INSERT INTO paper_account_transactions
                    (account_id, trade_date, kind, amount, cash_after, note)
                VALUES (:aid, :d, 'deposit', :amount, :cash, 'forward ledger opening balance')
            """), {"aid": row[0], "d": date.today(), "amount": opening_cash,
                    "cash": opening_cash})


def locked_account(session, strategy_key: str = SWING_ACCOUNT_KEY):
    return session.execute(text("""
        SELECT id, initial_capital, cash FROM paper_accounts
        WHERE strategy_key=:key FOR UPDATE
    """), {"key": strategy_key}).fetchone()


def marked_nav(session, cash: float, account_id: int | None = None) -> float:
    """NAV of the forward ledger only; legacy quantity-less positions are excluded."""
    account_filter = " AND p.paper_account_id=:aid" if account_id is not None else ""
    value = session.execute(text(f"""
        SELECT COALESCE(SUM(p.shares * COALESCE(
            (SELECT d.close FROM daily_prices d WHERE d.stock_id=p.stock_id
             AND d.close > 0 ORDER BY d.trade_date DESC LIMIT 1), p.entry_price)), 0)
        FROM positions p
        WHERE p.status='open' AND COALESCE(p.source,'ai')='ai' AND p.shares IS NOT NULL
        {account_filter}
    """), {"aid": account_id} if account_id is not None else {}).scalar()
    return float(cash) + float(value or 0)


def paper_account_snapshot(strategy_key: str = SWING_ACCOUNT_KEY) -> dict | None:
    """Return an honest forward-ledger snapshot plus legacy coverage diagnostics."""
    ensure_paper_account()
    with get_session() as s:
        account = s.execute(text("""
            SELECT id, initial_capital, cash, last_nav, started_at, updated_at
            FROM paper_accounts WHERE strategy_key=:key
        """), {"key": strategy_key}).fetchone()
        if account is None:
            return None
        aid, initial, cash, _, started, updated = account
        tracked = s.execute(text("""
            SELECT COUNT(*), COALESCE(SUM(p.shares * COALESCE(
                (SELECT d.close FROM daily_prices d WHERE d.stock_id=p.stock_id
                 AND d.close > 0 ORDER BY d.trade_date DESC LIMIT 1), p.entry_price)), 0)
            FROM positions p
            WHERE p.status='open' AND COALESCE(p.source,'ai')='ai'
              AND p.paper_account_id=:aid AND p.shares IS NOT NULL
        """), {"aid": aid}).fetchone()
        legacy = s.execute(text("""
            SELECT COUNT(*), ARRAY_AGG(stock_id ORDER BY entry_date)
            FROM positions
            WHERE status='open' AND COALESCE(source,'ai')='ai'
              AND (paper_account_id IS DISTINCT FROM :aid OR shares IS NULL)
        """), {"aid": aid}).fetchone()
        tracked_value = float(tracked[1] or 0)
        nav = float(cash) + tracked_value
        return {
            "account_id": int(aid), "initial_capital": float(initial),
            "cash": float(cash), "tracked_market_value": tracked_value,
            "tracked_nav": nav, "tracked_open_count": int(tracked[0] or 0),
            "legacy_open_count": int(legacy[0] or 0),
            "legacy_stock_ids": list(legacy[1] or []),
            "all_ai_positions_covered": int(legacy[0] or 0) == 0,
            "started_at": started, "updated_at": updated,
        }


def record_cash(session, account_id: int, position_id: int | None, trade_date: date,
                kind: str, amount: float, cash_after: float, note: str = "") -> None:
    session.execute(text("""
        INSERT INTO paper_account_transactions
            (account_id, position_id, trade_date, kind, amount, cash_after, note)
        VALUES (:aid, :pid, :d, :kind, :amount, :cash, :note)
    """), {"aid": account_id, "pid": position_id, "d": trade_date,
            "kind": kind, "amount": round(amount, 2), "cash": round(cash_after, 2),
            "note": note})


def update_account(session, account_id: int, cash: float, nav: float) -> None:
    session.execute(text("""
        UPDATE paper_accounts SET cash=:cash, last_nav=:nav, updated_at=NOW() WHERE id=:aid
    """), {"cash": round(cash, 2), "nav": round(nav, 2), "aid": account_id})
