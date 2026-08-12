"""MOM-1 portfolio construction and execution correctness primitives.

This module completes signal-to-order F0 mechanics without calculating returns,
NAV paths, or performance.  Inputs are explicit point-in-time values supplied by
the caller; unavailable execution state is deferred or cancelled, never filled
with an invented price.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import math
from numbers import Integral
from typing import Iterable, Mapping

import pandas as pd

from agent.strategy import (
    FEE_RATE,
    TAX_RATE,
    buy_fill,
    sell_fill,
    split_order_quantity,
)
from research.momentum import (
    ENTRY_TOP_FRAC,
    EXIT_BOTTOM_FRAC,
    _cutoff,
    _trading_index,
    deterministic_rank,
    next_session,
)


MAX_POSITIONS = 10
MAX_PER_INDUSTRY = 3
TARGET_WEIGHT = 0.10
MAX_AVG_VOLUME_FRACTION = 0.01


def pit_industry_map(
    market_structure: pd.DataFrame,
    as_of,
    stock_ids: Iterable[str],
) -> pd.Series:
    """Return the latest non-future, officially PIT industry code per stock.

    Missing/non-PIT classifications remain ``pd.NA``.  A future observation in
    a snapshot dated on or before ``as_of`` is rejected instead of silently
    leaking it into the sector cap.
    """
    required = {
        "snapshot_date",
        "stock_id",
        "industry_code_asof",
        "industry_is_point_in_time",
    }
    missing = required - set(market_structure.columns)
    if missing:
        raise ValueError(f"market structure 缺少必要欄位: {sorted(missing)}")

    ids = pd.Index([str(stock_id) for stock_id in stock_ids], dtype=object)
    if ids.has_duplicates:
        raise ValueError("stock_ids 不可重複")
    frame = market_structure.copy()
    frame["snapshot_date"] = pd.to_datetime(frame["snapshot_date"], errors="coerce")
    frame["stock_id"] = frame["stock_id"].astype(str)
    if frame[["snapshot_date", "stock_id"]].isna().any().any():
        raise ValueError("market structure snapshot_date/stock_id 不可為空")

    as_of = pd.Timestamp(as_of)
    past = frame[frame["snapshot_date"] <= as_of]
    result = pd.Series(pd.NA, index=ids, dtype="string", name="industry_code_asof")
    if past.empty:
        return result
    latest_date = past["snapshot_date"].max()
    latest = past[past["snapshot_date"] == latest_date].copy()
    if latest["stock_id"].duplicated().any():
        raise ValueError("同一 market-structure snapshot 的 stock_id 必須唯一")

    valid = latest["industry_is_point_in_time"].fillna(False).astype(bool)
    if "industry_observation_date" in latest.columns:
        observation = pd.to_datetime(latest["industry_observation_date"], errors="coerce")
        future = observation.notna() & (observation > as_of)
        if future.any():
            raise ValueError("industry observation 晚於 as_of；不得以前視方式使用")
        valid &= observation.notna()
    lookup = latest.loc[valid].set_index("stock_id")["industry_code_asof"].astype("string")
    result.loc[:] = lookup.reindex(ids)
    return result


def select_holdings_with_industry_cap(
    signal_values: pd.Series,
    industry_by_stock: pd.Series,
    previous_holdings: Iterable[str] = (),
    *,
    max_positions: int = MAX_POSITIONS,
    max_per_industry: int = MAX_PER_INDUSTRY,
    require_complete_industry: bool = False,
) -> list[str]:
    """Apply the frozen 10%/20% buffer and 3-name PIT sector cap together.

    Existing positions still in the top 20% retain priority.  All qualifying
    top-10% entrants remain available to fill slots skipped by the sector cap;
    applying the cap only after truncating to ten would incorrectly leave cash.
    Unknown industries are excluded by the frozen formal policy; strict audit
    mode can still raise to quantify the underlying data gap.
    """
    for name, value in (
        ("max_positions", max_positions),
        ("max_per_industry", max_per_industry),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} 必須是非負整數")
    if max_positions == 0 or max_per_industry == 0:
        return []

    rank = deterministic_rank(signal_values)
    entry_cutoff = _cutoff(len(rank), ENTRY_TOP_FRAC)
    keep_cutoff = _cutoff(len(rank), EXIT_BOTTOM_FRAC)
    held = {str(stock_id) for stock_id in previous_holdings}
    keep = [sid for sid in rank.index if str(sid) in held and rank[sid] <= keep_cutoff]
    entrants = [sid for sid in rank.index if rank[sid] <= entry_cutoff and sid not in keep]
    candidates = sorted(keep, key=rank.get) + sorted(entrants, key=rank.get)

    industry = industry_by_stock.reindex(candidates)
    missing_ids = [str(sid) for sid in industry.index[industry.isna()]]
    if missing_ids and require_complete_industry:
        sample = ", ".join(missing_ids[:10])
        raise ValueError(f"PIT 產業缺值，§7.4 阻擋正式選股: {sample}")

    counts: Counter[str] = Counter()
    selected: list[str] = []
    for stock_id in candidates:
        code = industry.get(stock_id)
        if pd.isna(code):
            continue
        code = str(code)
        if counts[code] >= max_per_industry:
            continue
        selected.append(str(stock_id))
        counts[code] += 1
        if len(selected) == max_positions:
            break
    return selected


def equal_weight_target_shares(
    *,
    executable_price: float,
    nav: float,
    average_volume_shares: float,
    target_weight: float = TARGET_WEIGHT,
    maximum_average_volume_fraction: float = MAX_AVG_VOLUME_FRACTION,
) -> int:
    """Return integer shares under equal-weight notional and 1% volume caps."""
    values = {
        "executable_price": executable_price,
        "nav": nav,
        "average_volume_shares": average_volume_shares,
        "target_weight": target_weight,
        "maximum_average_volume_fraction": maximum_average_volume_fraction,
    }
    for name, value in values.items():
        if value is None or not math.isfinite(float(value)):
            raise ValueError(f"{name} 必須是有限數值")
    if executable_price <= 0 or nav < 0 or average_volume_shares < 0:
        raise ValueError("price 必須 > 0；nav 與 average volume 不可為負")
    if not 0 <= target_weight <= 1 or not 0 <= maximum_average_volume_fraction <= 1:
        raise ValueError("target weight 與 volume fraction 必須介於 0 和 1")

    shares_by_notional = math.floor(nav * target_weight / executable_price)
    shares_by_liquidity = math.floor(
        average_volume_shares * maximum_average_volume_fraction
    )
    return max(min(shares_by_notional, shares_by_liquidity), 0)


@dataclass(frozen=True)
class RebalanceOrder:
    stock_id: str
    side: str
    shares: int
    current_shares: int
    target_shares: int
    raw_open_price: float
    executable_price: float
    gross_notional_twd: float
    commission_twd: float
    transaction_tax_twd: float
    cash_delta_twd: float
    common_lots: int
    odd_lot_shares: int

    def to_dict(self) -> dict:
        return asdict(self)


def build_equal_weight_rebalance_orders(
    *,
    target_holdings: Iterable[str],
    current_shares: Mapping[str, int],
    raw_open_prices: pd.Series,
    average_volumes_shares: pd.Series,
    nav: float,
) -> list[RebalanceOrder]:
    """Build deterministic sell-first orders with shared fee/tax/slippage costs."""
    targets = [str(stock_id) for stock_id in target_holdings]
    if len(targets) != len(set(targets)):
        raise ValueError("target_holdings 不可重複")
    if len(targets) > MAX_POSITIONS:
        raise ValueError(f"target_holdings 不可超過 {MAX_POSITIONS} 檔")

    current: dict[str, int] = {}
    for stock_id, shares in current_shares.items():
        if isinstance(shares, bool) or not isinstance(shares, Integral) or shares < 0:
            raise ValueError("current shares 必須是非負整數股")
        current[str(stock_id)] = int(shares)

    target_quantities: dict[str, int] = {}
    for stock_id in targets:
        if stock_id not in raw_open_prices.index:
            raise ValueError(f"缺少 {stock_id} raw open price")
        if stock_id not in average_volumes_shares.index:
            raise ValueError(f"缺少 {stock_id} average volume")
        raw_open = float(raw_open_prices[stock_id])
        target_quantities[stock_id] = equal_weight_target_shares(
            executable_price=buy_fill(raw_open),
            nav=nav,
            average_volume_shares=float(average_volumes_shares[stock_id]),
        )

    orders: list[RebalanceOrder] = []
    order_ids = sorted(set(current) | set(targets))
    for stock_id in order_ids:
        old = current.get(stock_id, 0)
        target = target_quantities.get(stock_id, 0)
        delta = target - old
        if delta == 0:
            continue
        if stock_id not in raw_open_prices.index:
            raise ValueError(f"缺少 {stock_id} raw open price")
        raw_open = float(raw_open_prices[stock_id])
        if not math.isfinite(raw_open) or raw_open <= 0:
            raise ValueError(f"{stock_id} raw open price 必須是有限正數")
        side = "buy" if delta > 0 else "sell"
        price = buy_fill(raw_open) if side == "buy" else sell_fill(raw_open)
        gross = abs(delta) * price
        commission = gross * FEE_RATE
        tax = gross * TAX_RATE if side == "sell" else 0.0
        cash_delta = -(gross + commission) if side == "buy" else gross - commission - tax
        units = split_order_quantity(abs(delta))
        orders.append(RebalanceOrder(
            stock_id=stock_id,
            side=side,
            shares=abs(delta),
            current_shares=old,
            target_shares=target,
            raw_open_price=raw_open,
            executable_price=price,
            gross_notional_twd=gross,
            commission_twd=commission,
            transaction_tax_twd=tax,
            cash_delta_twd=cash_delta,
            common_lots=units["common_lots"],
            odd_lot_shares=units["odd_lot_shares"],
        ))
    return sorted(orders, key=lambda order: (order.side != "sell", order.stock_id))


@dataclass(frozen=True)
class ExecutionAttempt:
    status: str
    side: str
    attempt_date: str | None
    executable_price: float | None
    deferred_to: str | None
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_execution_attempt(
    *,
    side: str,
    decision_date,
    attempt_date,
    trading_days: Iterable,
    open_price: float | None,
    suspended: bool = False,
    locked_limit: str | None = None,
    valid_until=None,
) -> ExecutionAttempt:
    """Evaluate a T+1-or-later open attempt without inventing a fill.

    A blocked attempt is deferred to the next actual session.  At sample end or
    after ``valid_until`` (normally the next monthly decision) it is cancelled.
    Locked limit-up blocks buys; locked limit-down blocks sells.
    """
    if side not in {"buy", "sell"}:
        raise ValueError("side 必須是 buy 或 sell")
    if locked_limit not in {None, "up", "down"}:
        raise ValueError("locked_limit 必須是 None、up 或 down")
    calendar = _trading_index(trading_days)
    first = next_session(calendar, decision_date)
    if first is None:
        return ExecutionAttempt("cancelled", side, None, None, None, "sample_end")

    attempt = pd.Timestamp(attempt_date)
    if attempt not in calendar or attempt < first:
        raise ValueError("attempt_date 必須是 decision_date 之後的實際交易日")
    expiry = None if valid_until is None else pd.Timestamp(valid_until)
    if expiry is not None and expiry <= pd.Timestamp(decision_date):
        raise ValueError("valid_until 必須晚於 decision_date")
    if expiry is not None and attempt > expiry:
        return ExecutionAttempt(
            "cancelled", side, attempt.date().isoformat(), None, None, "superseded_signal"
        )

    blocked_reason: str | None = None
    if suspended:
        blocked_reason = "suspended"
    elif open_price is None or not math.isfinite(float(open_price)) or float(open_price) <= 0:
        blocked_reason = "missing_open"
    elif (side == "buy" and locked_limit == "up") or (
        side == "sell" and locked_limit == "down"
    ):
        blocked_reason = f"locked_limit_{locked_limit}"

    if blocked_reason is None:
        return ExecutionAttempt(
            "executable", side, attempt.date().isoformat(), float(open_price), None, "ok"
        )
    following = next_session(calendar, attempt)
    if following is None or (expiry is not None and following > expiry):
        return ExecutionAttempt(
            "cancelled",
            side,
            attempt.date().isoformat(),
            None,
            None,
            "superseded_signal" if following is not None else blocked_reason,
        )
    return ExecutionAttempt(
        "deferred",
        side,
        attempt.date().isoformat(),
        None,
        following.date().isoformat(),
        blocked_reason,
    )
