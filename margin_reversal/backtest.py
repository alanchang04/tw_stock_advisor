"""Cash-constrained, next-open backtest for the independent short-term strategy."""
from __future__ import annotations

import pandas as pd

from agent.backtest import perf_metrics
from .config import MARGIN_REVERSAL_CONFIG


def run_margin_reversal_backtest(prices: pd.DataFrame, signals: pd.DataFrame,
                                 cfg: dict | None = None) -> dict:
    cfg = {**MARGIN_REVERSAL_CONFIG, **(cfg or {})}
    px = prices.copy()
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    px = px.sort_values(["trade_date", "stock_id"])
    dates = list(pd.DatetimeIndex(px["trade_date"].unique()).sort_values())
    by_day = {(r.trade_date, str(r.stock_id)): r for r in px.itertuples()}
    sig = signals.copy()
    sig["trade_date"] = pd.to_datetime(sig["trade_date"])
    pending = {}
    pending_exits = {}
    cash = float(cfg["capital"])
    positions, trades, nav = {}, [], {}
    fee, tax, slip = cfg["fee_rate"], cfg["tax_rate"], cfg["slippage"]

    for day_i, day in enumerate(dates):
        # Exit decisions made at yesterday's close fill at today's open.
        for sid, reason in list(pending_exits.items()):
            p = positions.get(sid)
            bar = by_day.get((day, sid))
            if p is None:
                pending_exits.pop(sid, None); continue
            if bar is None or pd.isna(bar.open) or bar.open <= 0:
                continue
            fill = float(bar.open) * (1 - slip)
            proceeds = p["shares"] * fill * (1 - fee - tax)
            cash += proceeds
            trades.append({"stock_id": sid, "signal_date": p["signal_date"],
                           "entry_date": p["entry_date"], "exit_date": day,
                           "entry_price": p["entry_price"], "exit_price": fill,
                           "shares": p["shares"], "net_pnl": proceeds - p["cost"],
                           "net_return": proceeds / p["cost"] - 1, "reason": reason})
            del positions[sid]; pending_exits.pop(sid, None)

        # Signals observed yesterday fill at today's open.
        for sid, signal_day in list(pending.items()):
            if sid in positions or len(positions) >= cfg["max_positions"]:
                pending.pop(sid, None); continue
            bar = by_day.get((day, sid))
            if bar is None or pd.isna(bar.open) or bar.open <= 0:
                continue
            fill = float(bar.open) * (1 + slip)
            marks = sum(p["shares"] * p["last"] for p in positions.values())
            equity = cash + marks
            budget = min(cash, equity * cfg["position_fraction"])
            shares = int(budget // (fill * (1 + fee)))
            if shares <= 0:
                pending.pop(sid, None); continue
            cost = shares * fill * (1 + fee)
            cash -= cost
            positions[sid] = {"entry_date": day, "entry_price": fill, "shares": shares,
                              "cost": cost, "entry_i": day_i, "last": fill,
                              "signal_date": signal_day}
            pending.pop(sid, None)

        for sid, p in list(positions.items()):
            bar = by_day.get((day, sid))
            if bar is None or pd.isna(bar.close):
                continue
            close = float(bar.close); p["last"] = close
            ret = close / p["entry_price"] - 1
            held = day_i - p["entry_i"]
            reason = ("stop_loss" if ret <= -cfg["stop_loss"] else
                      "take_profit" if ret >= cfg["take_profit"] else
                      "max_hold" if held >= cfg["max_hold_days"] else None)
            if reason and sid not in pending_exits:
                pending_exits[sid] = reason

        todays = sig[sig["trade_date"].eq(day)].sort_values("score", ascending=False)
        for sid in todays["stock_id"].astype(str):
            if sid not in positions and sid not in pending:
                pending[sid] = day
        nav[day] = cash + sum(p["shares"] * p["last"] for p in positions.values())

    if dates:
        day = dates[-1]
        for sid, p in list(positions.items()):
            proceeds = p["shares"] * p["last"] * (1 - slip) * (1 - fee - tax)
            cash += proceeds
            trades.append({"stock_id": sid, "signal_date": p["signal_date"],
                           "entry_date": p["entry_date"], "exit_date": day,
                           "entry_price": p["entry_price"], "exit_price": p["last"]*(1-slip),
                           "shares": p["shares"], "net_pnl": proceeds-p["cost"],
                           "net_return": proceeds/p["cost"]-1, "reason": "period_end"})
        nav[day] = cash
    nav_s = pd.Series(nav, dtype=float).sort_index()
    return {"trades": pd.DataFrame(trades), "nav": nav_s,
            "metrics": perf_metrics(nav_s), "ending_cash": cash}
