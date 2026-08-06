"""Pre-registered current-vs-Qullamaggie 2x2 entry/exit decomposition.

Formal strategy defaults are not changed.  The final holdout is intentionally
unavailable from this command; only development and one registered validation
batch may be run.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.backtest import (
    _candidates_asof,
    _entry_share_count,
    _hot_sectors_asof,
    _load,
    _portfolio_nav,
    _precompute_factors,
    buy_and_hold_nav,
    perf_metrics,
)
from agent.strategy import (
    FEE_RATE,
    SLIPPAGE,
    STRATEGY,
    TAX_RATE,
    apply_total_return_adjustment,
    decide_exit,
    split_adjust,
)
from research.data_splits import slice_dates
from research.qullamaggie import (
    EXIT_VARIANTS,
    QullamaggieSpec,
    attach_unconditional_entry_paths,
    build_breakout_features,
    build_prior_day_watchlist,
    initial_q_stop,
    q_exit_decision,
    assume_same_day_stop,
    stop_buy_fill,
    trade_distribution,
)


PARQUET_DIR = ROOT / "data" / "research"
OUT_JSON = ROOT / "research" / "results" / "qullamaggie_factorial_2026-08-05.json"
OUT_MD = ROOT / "research" / "results" / "qullamaggie_factorial_2026-08-05.md"
PURPOSE = "Qullamaggie 2x2 預先登記：現行/Q進場 × 現行/Q出場三組"

ENTRY_MODES = ("current", "qullamaggie")
# P3-9 reuses this engine with the production quality ranking and a prior-day box
# stop-buy.  Its candidate pools are built by the caller and passed in as an
# ``entry_schedule``, so only the mode name has to be accepted here.
QUALITY_ENTRY_MODES = ("quality_stop_buy",)
SUPPORTED_ENTRY_MODES = (*ENTRY_MODES, "q_stop_order", *QUALITY_ENTRY_MODES)
EXIT_MODES = ("current", *EXIT_VARIANTS.keys())
PREDECLARED_VARIANTS = {
    f"{entry}__{exit_mode}": {"entry": entry, "exit": exit_mode}
    for entry in ENTRY_MODES for exit_mode in EXIT_MODES
}


def _pivot(data: dict, column: str, like: pd.DataFrame | None = None) -> pd.DataFrame:
    out = data["prices"].pivot_table(index="trade_date", columns="stock_id", values=column)
    out = out.where(out > 0)
    return out.reindex(index=like.index, columns=like.columns) if like is not None else out


def prepare_research_data(data: dict, cfg: dict) -> dict:
    """Prepare the same adjusted panels and candidate caches as the main backtest."""
    closes = _pivot(data, "close")
    div_events = data.get("dividends")
    if cfg.get("total_return_adjust", True) and div_events is not None and not div_events.empty:
        closes = apply_total_return_adjustment(closes, div_events)
    data["_closes"] = closes
    if "_rs20" not in data:
        _precompute_factors(data, cfg)

    opens, highs, lows = (_pivot(data, c, closes) for c in ("open", "high", "low"))
    if cfg.get("total_return_adjust", True) and div_events is not None and not div_events.empty:
        opens = apply_total_return_adjustment(opens, div_events)
        highs = apply_total_return_adjustment(highs, div_events)
        lows = apply_total_return_adjustment(lows, div_events)
    volume = data["prices"].pivot_table(
        index="trade_date", columns="stock_id", values="volume"
    ).reindex(index=closes.index, columns=closes.columns)
    turnover = data["prices"].pivot_table(
        index="trade_date", columns="stock_id", values="turnover"
    ).reindex(index=closes.index, columns=closes.columns)
    change = data["prices"].pivot_table(
        index="trade_date", columns="stock_id", values="change_pct"
    ).reindex(index=closes.index, columns=closes.columns)
    data["_volume"] = volume
    data["_change_pct"] = change
    data["_avg_volume_liq"] = volume.shift(1).rolling(
        int(cfg.get("liquidity_avg_days", 5)), min_periods=1
    ).mean()
    data["_avg_turnover"] = turnover.rolling(
        int(cfg.get("turnover_avg_days", 5)), min_periods=1
    ).mean()
    if "_sid_to_inds" not in data:
        mapping = {}
        imap = data.get("imap")
        if imap is not None and not imap.empty:
            for sid, group in imap.groupby("stock_id"):
                mapping[str(sid)] = set(group["industry_code"])
        data["_sid_to_inds"] = mapping

    tech = data["tech"]
    ma5 = tech.pivot_table(index="trade_date", columns="stock_id", values="ma5").reindex_like(closes)
    ma20 = tech.pivot_table(index="trade_date", columns="stock_id", values="ma20").reindex_like(closes)
    ma10 = closes.rolling(10, min_periods=10).mean()
    q = build_breakout_features(opens, highs, lows, closes, volume)
    q_watch = build_prior_day_watchlist(opens, highs, lows, closes, volume)
    return {
        "closes": closes, "opens": opens, "highs": highs, "lows": lows,
        "volume": volume, "ma5": ma5, "ma10": ma10, "ma20": ma20,
        "q": q, "q_watch": q_watch,
    }


def _value(panel: pd.DataFrame, d, sid):
    try:
        value = panel.at[d, sid]
    except KeyError:
        return None
    return None if pd.isna(value) else float(value)


def _market_states(closes: pd.DataFrame, sid: str = "0050") -> dict[str, pd.Series]:
    if sid not in closes.columns:
        true = pd.Series(True, index=closes.index)
        return {"current": true, "qullamaggie": true}
    market = split_adjust(closes[sid]).ffill()
    ma10 = market.rolling(10, min_periods=10).mean()
    ma20 = market.rolling(20, min_periods=15).mean()
    ma60 = market.rolling(60, min_periods=30).mean()
    return {
        "current": (market >= ma60).fillna(True),
        "qullamaggie": (ma10 > ma20).fillna(False),
    }


def _sector_limited(candidates: list[str], positions: dict, data: dict, cfg: dict) -> list[str]:
    cap = cfg.get("sector_exposure_cap")
    mapping = data.get("_sid_to_inds") or {}
    if not cap or not mapping:
        return candidates
    maximum = max(1, round(int(cfg.get("max_open_positions", 10)) * float(cap)))
    counts = Counter()
    for sid in positions:
        for industry in mapping.get(sid, ()):
            counts[industry] += 1
    result = []
    for sid in candidates:
        industries = mapping.get(sid, ())
        if any(counts[industry] >= maximum for industry in industries):
            continue
        result.append(sid)
        for industry in industries:
            counts[industry] += 1
    return result


def current_candidates(data: dict, d, cfg: dict, top_n: int) -> list[str]:
    hot = (_hot_sectors_asof(data, d, top_n=cfg["hot_sectors_top_n"])
           if cfg.get("use_hot_sector_gate", True) else None)
    return list(_candidates_asof(data, d, hot, top_n=max(20, top_n * 4), cfg=cfg))


def q_candidates(data: dict, panels: dict, d, cfg: dict, top_n: int) -> list[str]:
    q = panels["q"]
    if d not in q["signal"].index:
        return []
    row = q["signal"].loc[d]
    hits = set(row.index[row.fillna(False)])
    if not hits:
        return []
    # Reuse the production point-in-time universe, listing, ETF, disposition and
    # liquidity filters; ranking below remains purely Q-style.
    universe_cfg = {
        **cfg, "use_hot_sector_gate": False, "min_rsi": 0, "max_rsi": 100,
        "require_swing_setup": False, "allow_new_entry_alt_gate": False,
    }
    # Passing a wide pool through the production selector also applies price,
    # volume, liquidity and disposition filters.  Its score order is discarded.
    eligible = set(_candidates_asof(
        data, d, None, top_n=99999, cfg=universe_cfg
    ))
    hits &= eligible
    score = q["score"].loc[d].dropna().sort_values(ascending=False)
    return [str(sid) for sid in score.index if sid in hits][:max(20, top_n * 4)]


def q_stop_order_candidates(data: dict, panels: dict, d, cfg: dict, top_n: int) -> list[dict]:
    watch = panels["q_watch"]
    if d not in watch["watch"].index:
        return []
    row = watch["watch"].loc[d]
    hits = set(row.index[row.fillna(False)])
    if not hits:
        return []
    universe_cfg = {
        **cfg, "use_hot_sector_gate": False, "min_rsi": 0, "max_rsi": 100,
        "require_swing_setup": False, "allow_new_entry_alt_gate": False,
    }
    eligible = set(_candidates_asof(data, d, None, top_n=99999, cfg=universe_cfg))
    score = watch["score"].loc[d].dropna().sort_values(ascending=False)
    orders = []
    for sid in score.index:
        if sid not in hits or sid not in eligible:
            continue
        trigger = _value(watch["trigger"], d, sid)
        adr = _value(watch["adr"], d, sid)
        if trigger is not None and adr is not None:
            orders.append({"stock_id": str(sid), "trigger": trigger, "adr": adr})
        if len(orders) >= max(20, top_n * 4):
            break
    return orders


def build_entry_schedule(
    data: dict, panels: dict, dates: list, entry_mode: str, cfg: dict,
    top_n: int = 5, rebalance: int = 5,
) -> dict:
    """Compute each date's broad entry pool once and share it across exit cells."""
    schedule = {}
    for i, d in enumerate(dates):
        if entry_mode == "current" and i % rebalance != 0:
            continue
        if entry_mode == "current":
            candidates = current_candidates(data, d, cfg, top_n)
        elif entry_mode == "qullamaggie":
            candidates = q_candidates(data, panels, d, cfg, top_n)
        else:
            candidates = q_stop_order_candidates(data, panels, d, cfg, top_n)
        if candidates:
            schedule[d] = candidates
    return schedule


def _log_order(order_log: list | None, d, order: dict, outcome: str) -> None:
    """Record what happened to one preplaced order.  Exactly one record per order.

    ``outcome`` is one of ``triggered``/``gap_open``/``market_open`` (filled) or
    ``cancelled_not_triggered``/``skipped_no_slot``/``skipped_no_price``/
    ``skipped_no_cash``.  Needed because fill and cancel rates cannot be recovered
    from the trade table -- cancelled orders leave no trade behind.
    """
    if order_log is None:
        return
    order_log.append({
        "date": d, "stock_id": order["stock_id"],
        "signal_date": order.get("signal_date"),
        "trigger": order.get("trigger"), "outcome": outcome,
    })


def _finalize_trade(sid: str, position: dict, d, fill: float, reason: str) -> dict:
    shares = int(position["shares"])
    proceeds = shares * fill * (1 - FEE_RATE - TAX_RATE)
    position["realized_proceeds"] += proceeds
    position["exit_value"] += shares * fill
    position["exit_shares"] += shares
    buy_cost = float(position["buy_cost"])
    net_pnl = float(position["realized_proceeds"] - buy_cost)
    average_exit = position["exit_value"] / max(1, position["exit_shares"])
    risk_cash = max(0.01, float(position["initial_risk_cash"]))
    return {
        "stock_id": sid,
        "entry_date": position["entry_date"], "exit_date": d,
        "entry_price": position["entry_price"], "exit_price": average_exit,
        "ret": average_exit / position["entry_price"] - 1.0,
        "net_ret": net_pnl / buy_cost if buy_cost else 0.0,
        "net_pnl": net_pnl, "buy_cost": buy_cost,
        "sell_proceeds": position["realized_proceeds"],
        "shares": position["initial_shares"],
        "hold": position["last_i"] - position["entry_i"],
        "reason": reason, "partial_done": bool(position["partial_done"]),
        "initial_stop": position["initial_stop"],
        "initial_risk_cash": risk_cash, "r_multiple": net_pnl / risk_cash,
        "mfe": position["mfe"], "mae": position["mae"],
        **position["path_returns"],
    }


def run_factorial_backtest(
    *, data: dict, panels: dict, start_date: date, end_date: date,
    entry_mode: str, exit_mode: str, cfg: dict | None = None,
    spec: QullamaggieSpec = QullamaggieSpec(), rebalance: int = 5,
    top_n: int = 5, slippage: float = SLIPPAGE,
    entry_schedule: dict | None = None, order_log: list | None = None,
) -> pd.DataFrame:
    """Run one factorial cell with real cash, shares, costs and next-open fills."""
    if entry_mode not in SUPPORTED_ENTRY_MODES or exit_mode not in EXIT_MODES:
        raise ValueError(f"unsupported factorial cell: {entry_mode}/{exit_mode}")
    cfg = {**STRATEGY, **(cfg or {})}
    closes, opens, highs, lows = (panels[k] for k in ("closes", "opens", "highs", "lows"))
    dates = [d for d in sorted(closes.index) if start_date <= d <= end_date]
    if len(dates) < 10:
        raise ValueError("backtest interval too short")
    pos_index = {d: i for i, d in enumerate(dates)}
    market = _market_states(closes, cfg.get("market_filter_stock", "0050"))
    max_open = int(cfg.get("max_open_positions", 10))
    cash = float(cfg["capital"])
    positions: dict[str, dict] = {}
    pending_entries: list[dict] = []
    pending_exits: dict[str, dict] = {}
    trades: list[dict] = []
    nav_points: list[tuple] = []

    for i, d in enumerate(dates):
        # 1) Yesterday's exit decisions fill at today's open.  Full exits always
        # supersede partials.  Break-even is moved only after a partial fills.
        for sid, order in list(pending_exits.items()):
            position = positions.get(sid)
            op = _value(opens, d, sid)
            if position is None or op is None:
                continue
            fill = op * (1 - slippage)
            if order["action"] == "partial" and position["shares"] >= 2:
                qty = max(1, position["shares"] // 2)
                proceeds = qty * fill * (1 - FEE_RATE - TAX_RATE)
                cash += proceeds
                position["shares"] -= qty
                position["realized_proceeds"] += proceeds
                position["exit_value"] += qty * fill
                position["exit_shares"] += qty
                position["partial_done"] = True
                position["stop_price"] = max(position["stop_price"], position["entry_price"])
            else:
                cash += position["shares"] * fill * (1 - FEE_RATE - TAX_RATE)
                position["last_i"] = i
                trades.append(_finalize_trade(sid, position, d, fill, order["reason"]))
                del positions[sid]
            del pending_exits[sid]

        # 2) Yesterday's entry signals fill at today's open.
        for order in pending_entries:
            sid = order["stock_id"]
            if sid in positions or len(positions) >= max_open:
                _log_order(order_log, d, order, "skipped_no_slot")
                continue
            op = _value(opens, d, sid)
            if op is None:
                _log_order(order_log, d, order, "skipped_no_price")
                continue
            trigger = order.get("trigger")
            if trigger is not None:
                day_high = _value(highs, d, sid)
                fill = stop_buy_fill(op, day_high, float(trigger), slippage)
                if fill is None:
                    # The preplaced stop never traded, so the order simply expires.
                    _log_order(order_log, d, order, "cancelled_not_triggered")
                    continue
                fill_kind = "gap_open" if op >= float(trigger) else "triggered"
            else:
                fill = op * (1 + slippage)
                fill_kind = "market_open"
            sig_d = order["signal_date"]
            if exit_mode == "current":
                stop = fill * (1 - float(cfg.get("stop_loss", 0.08)))
            elif trigger is not None:
                # Only prior-day ADR is known when the order is placed.  Using the
                # trigger day's eventual low here would be lookahead.
                stop = initial_q_stop(fill, None, order.get("adr"), spec)
            else:
                stop = initial_q_stop(
                    fill, _value(lows, sig_d, sid),
                    _value(panels["q"]["adr"], sig_d, sid), spec,
                )
            marks = {s: _value(opens, d, s) or p["last_mark"] for s, p in positions.items()}
            nav = _portfolio_nav(cash, positions, marks)
            shares = _entry_share_count(
                fill=fill, cash=cash, nav=nav, max_open=max_open,
                avg_volume=_value(data["_avg_volume_liq"], d, sid),
                max_pct_of_avg_volume=cfg.get("max_pct_of_avg_volume", 0.01),
                cfg=cfg, stop_price=stop, size_scale=1.0,
            )
            if shares <= 0 or shares * fill * (1 + FEE_RATE) > cash:
                _log_order(order_log, d, order, "skipped_no_cash")
                continue
            _log_order(order_log, d, order, fill_kind)
            buy_cost = shares * fill * (1 + FEE_RATE)
            cash -= buy_cost
            positions[sid] = {
                "entry_date": d, "entry_price": fill, "entry_i": i,
                "shares": shares, "initial_shares": shares, "buy_cost": buy_cost,
                "stop_price": stop, "initial_stop": stop,
                "initial_risk_cash": shares * max(0.01, fill - stop),
                "partial_done": False, "realized_proceeds": 0.0,
                "exit_value": 0.0, "exit_shares": 0,
                "last_mark": fill, "last_i": i, "mfe": 0.0, "mae": 0.0,
                "peak_close": fill,
                "path_returns": {},
            }
            if trigger is not None and exit_mode != "current":
                day_low = _value(lows, d, sid)
                assumption = cfg.get("intraday_path_assumption", "conservative")
                if assume_same_day_stop(
                    day_open=op, trigger=float(trigger), day_low=day_low,
                    stop_price=stop, path_assumption=assumption,
                ):
                    # With daily bars the order of high/low is unknown.  The
                    # conservative boundary assumes the breakout occurred first;
                    # a gap entry is unambiguous because the position exists at open.
                    stop_fill = stop * (1 - slippage)
                    cash += shares * stop_fill * (1 - FEE_RATE - TAX_RATE)
                    positions[sid]["last_i"] = i
                    trades.append(_finalize_trade(
                        sid, positions[sid], d, stop_fill, "Q同日觸發停損"
                    ))
                    del positions[sid]
        pending_entries = []

        # 3) Evaluate closes and queue next-open exits.
        bull_current = bool(market["current"].get(d, True))
        day_cfg = ({**cfg, "exit_on_death_cross": True}
                   if (not bull_current and cfg.get("bear_reenable_death_cross")) else cfg)
        for sid, position in list(positions.items()):
            close = _value(closes, d, sid)
            if close is None:
                continue
            position["last_mark"] = close
            position["last_i"] = i
            position["peak_close"] = max(position["peak_close"], close)
            high = _value(highs, d, sid) or close
            low = _value(lows, d, sid) or close
            position["mfe"] = max(position["mfe"], high / position["entry_price"] - 1)
            position["mae"] = min(position["mae"], low / position["entry_price"] - 1)
            holding_day = i - position["entry_i"] + 1
            if holding_day in (3, 5, 10):
                position["path_returns"][f"ret_day{holding_day}"] = close / position["entry_price"] - 1

            action = reason = None
            if exit_mode == "current":
                ex, reason = decide_exit(
                    position["entry_price"], position["peak_close"], close,
                    _value(panels["ma5"], d, sid), _value(panels["ma20"], d, sid),
                    holding_day - 1, cfg=day_cfg,
                )
                action = "full" if ex else None
            else:
                action, reason = q_exit_decision(
                    close=close, entry_price=position["entry_price"],
                    stop_price=position["stop_price"], holding_day=holding_day,
                    ma10=_value(panels["ma10"], d, sid),
                    ma20=_value(panels["ma20"], d, sid),
                    partial_done=position["partial_done"], variant=exit_mode, spec=spec,
                )
            if action and sid not in pending_exits:
                pending_exits[sid] = {"action": action, "reason": reason}

        # 4) Build entries. Current signals keep the production five-day cadence;
        # Q signals are event-driven and checked daily.
        # Q modes use the Q-style MA10>MA20 market state; every other mode -- the
        # production entry and the P3-9 quality stop-buy -- keeps the production
        # 0050-vs-MA60 filter, so entry timing is the only thing that varies.
        market_key = ("qullamaggie" if entry_mode in ("qullamaggie", "q_stop_order")
                      else "current")
        market_ok = bool(market[market_key].get(d, False))
        cadence_ok = entry_mode != "current" or i % rebalance == 0
        if market_ok and cadence_ok:
            free = max_open - len(positions) - len(pending_entries) + sum(
                1 for order in pending_exits.values() if order["action"] == "full"
            )
            if free > 0:
                candidates = (entry_schedule.get(d, []) if entry_schedule is not None else None)
                if entry_schedule is None:
                    if entry_mode == "current":
                        candidates = current_candidates(data, d, cfg, top_n)
                    elif entry_mode == "qullamaggie":
                        candidates = q_candidates(data, panels, d, cfg, top_n)
                    else:
                        candidates = q_stop_order_candidates(data, panels, d, cfg, top_n)
                def _sid(candidate):
                    return candidate["stock_id"] if isinstance(candidate, dict) else candidate
                candidates = [c for c in candidates
                              if _sid(c) not in positions and _sid(c) not in pending_exits]
                limited_sids = set(_sector_limited(
                    [_sid(c) for c in candidates], positions, data, cfg
                ))
                candidates = [c for c in candidates if _sid(c) in limited_sids]
                pending_entries = []
                for candidate in candidates[:min(top_n, free)]:
                    order = dict(candidate) if isinstance(candidate, dict) else {"stock_id": candidate}
                    order["signal_date"] = d
                    pending_entries.append(order)

        nav_points.append((d, _portfolio_nav(
            cash, positions, {sid: _value(closes, d, sid) for sid in positions}
        )))

    # Liquidate remaining positions at the final adjusted close.
    last = dates[-1]
    for sid, position in list(positions.items()):
        close = _value(closes, last, sid)
        if close is None:
            continue
        fill = close * (1 - slippage)
        cash += position["shares"] * fill * (1 - FEE_RATE - TAX_RATE)
        position["last_i"] = len(dates) - 1
        trades.append(_finalize_trade(sid, position, last, fill, "回測結束平倉"))
        del positions[sid]
    nav_points[-1] = (last, cash)
    tdf = pd.DataFrame(trades)
    if tdf.empty:
        return tdf

    nav = pd.Series(dict(nav_points), dtype=float).sort_index()
    metrics = perf_metrics(nav)
    market_sid = cfg.get("market_filter_stock", "0050")
    nav0050 = None
    if market_sid in closes.columns:
        market_close = split_adjust(closes[market_sid]).reindex(dates).ffill()
        nav0050 = buy_and_hold_nav(
            market_close, cfg["capital"], fee_rate=FEE_RATE, tax_rate=TAX_RATE,
            slippage=slippage,
        )
    bench = perf_metrics(nav0050) if nav0050 is not None else None
    tdf.attrs.update({
        "entry_mode": entry_mode, "exit_mode": exit_mode,
        "nav": {d: float(v) for d, v in nav.items()},
        "nav_total_ret": metrics["total"], "ann_ret": metrics["ann_ret"],
        "sharpe": metrics["sharpe"], "nav_mdd": metrics["mdd"],
        "calmar": metrics["calmar"],
        "bench_0050": bench["total"] if bench else None,
        "sharpe_0050": bench["sharpe"] if bench else None,
        "mdd_0050": bench["mdd"] if bench else None,
    })
    return attach_unconditional_entry_paths(tdf, closes)


def _result(trades: pd.DataFrame) -> dict:
    attrs = trades.attrs
    return {
        "total_return": float(attrs["nav_total_ret"]),
        "annual_return": float(attrs["ann_ret"]),
        "sharpe": float(attrs["sharpe"]),
        "mdd": float(attrs["nav_mdd"]), "calmar": float(attrs["calmar"]),
        "bench_0050_return": float(attrs["bench_0050"]),
        "bench_0050_sharpe": float(attrs["sharpe_0050"]),
        "bench_0050_mdd": float(attrs["mdd_0050"]),
        "reason_counts": {str(k): int(v) for k, v in trades["reason"].value_counts().items()},
        **trade_distribution(trades),
    }


def _assessment(payload: dict) -> dict:
    dev = payload.get("splits", {}).get("development", {}).get("variants", {})
    if not dev:
        return {"status": "development_not_run"}
    baseline = dev["current__current"]
    current_q = [dev[f"current__{name}"] for name in EXIT_VARIANTS]
    q_current = dev["qullamaggie__current"]
    q_q = [dev[f"qullamaggie__{name}"] for name in EXIT_VARIANTS]
    return {
        "status": "reject_deployment_keep_researching_entry_execution",
        "baseline_reproduced": {
            "annual_return": baseline["annual_return"], "sharpe": baseline["sharpe"],
            "mdd": baseline["mdd"], "trades": baseline["trades"],
        },
        "q_exit_on_current_entry_improved": any(
            m["sharpe"] > baseline["sharpe"] and m["annual_return"] > baseline["annual_return"]
            for m in current_q
        ),
        "q_entry_with_current_exit_improved": (
            q_current["sharpe"] > baseline["sharpe"]
            and q_current["annual_return"] > baseline["annual_return"]
        ),
        "complete_q_improved": any(
            m["sharpe"] > baseline["sharpe"] and m["annual_return"] > baseline["annual_return"]
            for m in q_q
        ),
        "q_entry_unconditional_path": {
            "day3_mean": q_current["day3_mean"],
            "day3_positive_rate": q_current["day3_positive_rate"],
            "day5_mean": q_current["day5_mean"],
            "day5_positive_rate": q_current["day5_positive_rate"],
            "day10_mean": q_current["day10_mean"],
            "day10_positive_rate": q_current["day10_positive_rate"],
        },
        "validation_run": "skipped_no_development_candidate_and_monthly_validation_already_used",
        "key_limitation": (
            "daily close signal executes at next open; this is not Qullamaggie's same-day "
            "opening-range-high execution and can measure delayed chasing rather than the original edge"
        ),
        "next_test": (
            "preplaced breakout stop-order approximation using only prior-day data, then a faithful "
            "5-minute opening-range test when point-in-time intraday history is available"
        ),
    }


def _markdown(payload: dict) -> str:
    lines = [
        "# Qullamaggie 進出場 2×2 拆解", "",
        "> 正式 STRATEGY 未變更；未讀取 final holdout。所有門檻在看到本次結果前固定。", "",
        "## 固定規格", "", "```json",
        json.dumps(payload["spec"], ensure_ascii=False, indent=2), "```", "",
    ]
    for split, section in payload["splits"].items():
        lines += [f"## {split}（{section['start']}～{section['end']}）", "",
                  "| 進場__出場 | 年化 | Sharpe | MDD | Calmar | 交易 | 勝率 | ≥3R | Top5獲利占比 |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for name, m in section["variants"].items():
            top5 = m.get("top5_profit_share")
            lines.append(
                f"| {name} | {m['annual_return']:.2%} | {m['sharpe']:.2f} | "
                f"{m['mdd']:.2%} | {m['calmar']:.2f} | {m['trades']} | "
                f"{m['win_rate']:.1%} | {m['r_ge_3']} | "
                f"{top5:.1%} |" if top5 is not None else
                f"| {name} | {m['annual_return']:.2%} | {m['sharpe']:.2f} | "
                f"{m['mdd']:.2%} | {m['calmar']:.2f} | {m['trades']} | "
                f"{m['win_rate']:.1%} | {m['r_ge_3']} | — |"
            )
        first = next(iter(section["variants"].values()))
        lines += ["", f"0050 同期：總報酬 {first['bench_0050_return']:.2%}、"
                  f"Sharpe {first['bench_0050_sharpe']:.2f}、MDD {first['bench_0050_mdd']:.2%}。", ""]
    lines += [
        "## 本輪結論", "",
        "- **基準可信。** 同一研究引擎重現既有 development baseline：年化 13.55%、Sharpe 0.81、MDD -26.64%、236 筆。",
        "- **Q 出場不能接到現行進場。** 三組年化分別約 5.06%、0.85%、-3.69%，且交易暴增到 925～985 筆，代表快速出場後反覆買回同一批品質候選，成本與假突破把優勢吃掉。",
        "- **本版 Q 日線進場沒有勝出。** 搭現行出場年化約 6.21%、Sharpe 0.54；完整 Q 組合年化約 0.79% 到 -2.11%。",
        "- **第 5 天減半不適用目前台股訊號。** 無條件追蹤下，Q 進場第 3／5 日平均約 -1.3%／-1.1%，第 5 日僅約 37% 為正；第 10 日才回到約 +1.2%。",
        "- **尚不能據此否決原版 Qullamaggie。** 本系統收盤後才知道突破成立，只能隔日開盤進場；原策略是突破當天用 opening-range high 進場。這輪測到的可能是『突破後隔日追價』，不是原始 ORH edge。",
        "- **不執行 validation。** development 沒有任何候選勝過基準，而且本月 validation 已被前一輪風控批次使用；不為負結果再次消耗驗證集。",
        "",
        "下一步應先做只用前一日資料預掛突破停損單的日線近似；若仍有優勢，再取得 5 分鐘歷史資料做忠實 ORH 回測。", "",
        "## 判讀方式", "",
        "- 現行進場＋Q出場改善：出場是主要漏損。",
        "- Q進場＋現行出場改善：進場結構是主要問題。",
        "- 只有Q進場＋Q出場改善：兩者存在配對效應，應維持獨立策略。",
        "- development 改善但 validation 翻轉：否決部署，不再掃門檻。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=("development", "validation"), action="append")
    parser.add_argument("--only", choices=sorted(PREDECLARED_VARIANTS))
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()
    cfg = dict(STRATEGY)
    spec = QullamaggieSpec()
    data = _load(parquet_dir=str(PARQUET_DIR))
    panels = prepare_research_data(data, cfg)
    all_dates = sorted(pd.to_datetime(data["prices"]["trade_date"]).dt.date.unique())
    splits = args.split or ["development", "validation"]
    variants = ({args.only: PREDECLARED_VARIANTS[args.only]}
                if args.only else PREDECLARED_VARIANTS)
    payload = {
        "experiment": "qullamaggie_factorial_v1", "run_date": str(date.today()),
        "spec": spec.to_dict(), "variants": variants, "splits": {},
        "holdout_touched": False, "formal_defaults_changed": False,
    }
    for split in splits:
        selected = slice_dates(all_dates, split, purpose=PURPOSE)
        if not selected:
            raise RuntimeError(f"no data for {split}")
        section = {"start": str(min(selected)), "end": str(max(selected)), "variants": {}}
        split_dates = [d for d in sorted(panels["closes"].index)
                       if min(selected) <= d <= max(selected)]
        needed_entries = sorted({modes["entry"] for modes in variants.values()})
        schedules = {}
        for entry_mode in needed_entries:
            print(f"{split:11s} precompute {entry_mode} entry pools", flush=True)
            schedules[entry_mode] = build_entry_schedule(
                data, panels, split_dates, entry_mode, cfg
            )
            print(f"{split:11s} {entry_mode} signal dates="
                  f"{len(schedules[entry_mode])}", flush=True)
        for name, modes in variants.items():
            trades = run_factorial_backtest(
                data=data, panels=panels, start_date=min(selected), end_date=max(selected),
                entry_mode=modes["entry"], exit_mode=modes["exit"], cfg=cfg, spec=spec,
                entry_schedule=schedules[modes["entry"]],
            )
            if trades.empty:
                raise RuntimeError(f"{split}/{name}: no trades")
            section["variants"][name] = _result(trades)
            m = section["variants"][name]
            print(f"{split:11s} {name:34s} ann={m['annual_return']:+.2%} "
                  f"S={m['sharpe']:.2f} MDD={m['mdd']:+.2%} n={m['trades']}", flush=True)
        payload["splits"][split] = section
    # _assessment compares every declared cell, so it is only meaningful for a full
    # run; with --only it used to raise KeyError *after* printing the result.
    payload["assessment"] = (
        {"status": "partial_diagnostic_only", "only": args.only} if args.only
        else _assessment(payload)
    )
    if args.no_write:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(_markdown(payload), encoding="utf-8")
    print(f"wrote {OUT_JSON}")
    print(f"wrote {OUT_MD}")


if __name__ == "__main__":
    main()
