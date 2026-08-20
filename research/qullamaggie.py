"""Independent Qullamaggie-style breakout research.

This module deliberately does not mutate ``agent.strategy.STRATEGY``.  It provides
point-in-time breakout signals, exit-state decisions, and summary statistics that
can be tested independently before they are connected to a portfolio simulation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class QullamaggieSpec:
    prior_move_days: int = 60
    base_days: int = 20
    prior_move_min: float = 0.30
    rs_quantile: float = 0.90
    adr_days: int = 20
    adr_min: float = 0.04
    base_depth_max: float = 0.25
    breakout_volume_multiple: float = 1.50
    close_top_fraction: float = 0.33
    near_high_fraction: float = 0.80
    watch_distance_max: float = 0.05
    trail_start_day: int = 5
    partial_day: int = 5
    initial_risk_cap_adr: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)


EXIT_VARIANTS = {
    "q_full_10ma": {"partial": False, "trail_days": 10},
    "q_half_day5_10ma": {"partial": True, "trail_days": 10},
    "q_half_day5_20ma": {"partial": True, "trail_days": 20},
}


def _align(panel: pd.DataFrame, like: pd.DataFrame) -> pd.DataFrame:
    return panel.reindex(index=like.index, columns=like.columns)


def build_breakout_features(
    opens: pd.DataFrame,
    highs: pd.DataFrame,
    lows: pd.DataFrame,
    closes: pd.DataFrame,
    volumes: pd.DataFrame,
    spec: QullamaggieSpec = QullamaggieSpec(),
) -> dict[str, pd.DataFrame]:
    """Build point-in-time features and a mechanical breakout signal.

    Every base/high/volume comparison is shifted by one day.  The only unshifted
    values are the signal day's OHLCV, which are known after that close and are
    executed at the following open by the research runner.
    """
    opens, highs, lows, volumes = (
        _align(p, closes) for p in (opens, highs, lows, volumes)
    )
    prior = spec.prior_move_days
    base = spec.base_days

    # Prior move ends before the base begins, preventing the breakout bar from
    # manufacturing its own historical strength.
    prior_move = closes.shift(base) / closes.shift(base + prior) - 1.0
    ret60 = closes / closes.shift(prior) - 1.0
    rs_pct = ret60.rank(axis=1, pct=True)

    prev_close = closes.shift(1)
    adr = ((highs - lows) / prev_close.where(prev_close > 0)).rolling(
        spec.adr_days, min_periods=spec.adr_days
    ).mean().shift(1)

    base_high = highs.rolling(base, min_periods=base).max().shift(1)
    base_low = lows.rolling(base, min_periods=base).min().shift(1)
    base_depth = (base_high - base_low) / base_high.where(base_high > 0)
    half = max(2, base // 2)
    older_low = lows.shift(half).rolling(half, min_periods=half).min().shift(1)
    recent_low = lows.rolling(half, min_periods=half).min().shift(1)

    vol_avg = volumes.rolling(base, min_periods=base).mean().shift(1)
    ma10 = closes.rolling(10, min_periods=10).mean()
    ma20 = closes.rolling(20, min_periods=20).mean()
    high252 = highs.rolling(252, min_periods=120).max().shift(1)
    day_range = (highs - lows).where((highs - lows) > 0)
    close_position = (closes - lows) / day_range
    volume_ratio = volumes / vol_avg.where(vol_avg > 0)

    signal = (
        (prior_move >= spec.prior_move_min)
        & (rs_pct >= spec.rs_quantile)
        & (adr >= spec.adr_min)
        & (base_depth <= spec.base_depth_max)
        & (recent_low >= older_low)
        & (ma10 > ma20)
        & (ma20 > ma20.shift(5))
        & (closes >= high252 * spec.near_high_fraction)
        & (closes > base_high)
        & (volume_ratio >= spec.breakout_volume_multiple)
        & (closes > opens)
        & (close_position >= 1.0 - spec.close_top_fraction)
    ).fillna(False)

    # Ranking only orders simultaneous valid signals; it is not another gate.
    tightness = (1.0 - base_depth.clip(0, 1)).fillna(0.0)
    score = (
        rs_pct.fillna(0.0) * 0.40
        + tightness * 0.25
        + volume_ratio.clip(0, 3).fillna(0.0) / 3.0 * 0.20
        + (prior_move.clip(0, 1).fillna(0.0)) * 0.15
    ).where(signal)

    return {
        "signal": signal,
        "score": score,
        "adr": adr,
        "prior_move": prior_move,
        "rs_pct": rs_pct,
        "base_depth": base_depth,
        "base_high": base_high,
        "volume_ratio": volume_ratio,
        "ma10": ma10,
        "ma20": ma20,
    }


def build_prior_day_watchlist(
    opens: pd.DataFrame,
    highs: pd.DataFrame,
    lows: pd.DataFrame,
    closes: pd.DataFrame,
    volumes: pd.DataFrame,
    spec: QullamaggieSpec = QullamaggieSpec(),
) -> dict[str, pd.DataFrame]:
    """Build a watchlist and stop-buy level known at each day's close.

    Unlike :func:`build_breakout_features`, this does not require tomorrow's red
    candle, close location, or volume.  A trader can therefore place the returned
    trigger after today's close for execution during the next session.
    """
    opens, highs, lows, volumes = (
        _align(p, closes) for p in (opens, highs, lows, volumes)
    )
    prior, base = spec.prior_move_days, spec.base_days
    prior_move = closes.shift(base) / closes.shift(base + prior) - 1.0
    ret60 = closes / closes.shift(prior) - 1.0
    rs_pct = ret60.rank(axis=1, pct=True)
    prev_close = closes.shift(1)
    adr = ((highs - lows) / prev_close.where(prev_close > 0)).rolling(
        spec.adr_days, min_periods=spec.adr_days
    ).mean()
    trigger = highs.rolling(base, min_periods=base).max()
    base_low = lows.rolling(base, min_periods=base).min()
    base_depth = (trigger - base_low) / trigger.where(trigger > 0)
    half = max(2, base // 2)
    older_low = lows.shift(half).rolling(half, min_periods=half).min()
    recent_low = lows.rolling(half, min_periods=half).min()
    ma10 = closes.rolling(10, min_periods=10).mean()
    ma20 = closes.rolling(20, min_periods=20).mean()
    high252 = highs.rolling(252, min_periods=120).max()
    distance = trigger / closes.where(closes > 0) - 1.0

    watch = (
        (prior_move >= spec.prior_move_min)
        & (rs_pct >= spec.rs_quantile)
        & (adr >= spec.adr_min)
        & (base_depth <= spec.base_depth_max)
        & (recent_low >= older_low)
        & (ma10 > ma20)
        & (ma20 > ma20.shift(5))
        & (closes >= high252 * spec.near_high_fraction)
        & (distance >= 0)
        & (distance <= spec.watch_distance_max)
    ).fillna(False)
    tightness = (1.0 - base_depth.clip(0, 1)).fillna(0.0)
    score = (
        rs_pct.fillna(0.0) * 0.45
        + tightness * 0.30
        + prior_move.clip(0, 1).fillna(0.0) * 0.15
        + (1.0 - distance.clip(0, spec.watch_distance_max)
           / spec.watch_distance_max).fillna(0.0) * 0.10
    ).where(watch)
    return {
        "watch": watch, "score": score, "trigger": trigger, "adr": adr,
        "prior_move": prior_move, "rs_pct": rs_pct, "base_depth": base_depth,
        "distance": distance, "ma10": ma10, "ma20": ma20,
    }


def initial_q_stop(
    entry_price: float,
    signal_low: float | None,
    adr: float | None,
    spec: QullamaggieSpec = QullamaggieSpec(),
) -> float:
    """Signal-day low, capped so planned risk is never wider than one ADR."""
    fallback_adr = spec.adr_min
    adr_value = fallback_adr if adr is None or not np.isfinite(adr) else max(0.0, float(adr))
    adr_stop = entry_price * (1.0 - adr_value * spec.initial_risk_cap_adr)
    low = float(signal_low) if signal_low is not None and np.isfinite(signal_low) else adr_stop
    stop = max(low, adr_stop)
    if stop >= entry_price:
        stop = adr_stop
    return max(0.01, float(stop))


def stop_buy_fill(
    day_open: float | None,
    day_high: float | None,
    trigger: float,
    slippage: float,
) -> float | None:
    """Fill a preplaced buy-stop without using the day's close or low."""
    if day_open is None or day_high is None or day_high < trigger:
        return None
    return max(float(day_open), float(trigger)) * (1.0 + float(slippage))


def assume_same_day_stop(
    *, day_open: float, trigger: float, day_low: float | None,
    stop_price: float, path_assumption: str,
) -> bool:
    """Resolve the unobservable high/low order of a daily bar as explicit bounds."""
    if path_assumption not in ("conservative", "relaxed"):
        raise ValueError("path_assumption must be conservative or relaxed")
    if day_low is None or day_low > stop_price:
        return False
    if day_open >= trigger:
        return True  # position existed from the open, so the later low is actionable
    return path_assumption == "conservative"


def q_exit_decision(
    *,
    close: float,
    entry_price: float,
    stop_price: float,
    holding_day: int,
    ma10: float | None,
    ma20: float | None,
    partial_done: bool,
    variant: str,
    spec: QullamaggieSpec = QullamaggieSpec(),
) -> tuple[str | None, str | None]:
    """Return ``(action, reason)`` where action is ``full`` or ``partial``.

    Full-risk exits take priority.  A partial is only requested on/after day five
    while profitable; the portfolio runner moves the stop to break-even only after
    that partial order actually fills at the next open.
    """
    if variant not in EXIT_VARIANTS:
        raise ValueError(f"unknown Q exit variant: {variant}")
    if close <= stop_price:
        return "full", "Q初始/成本停損"

    rule = EXIT_VARIANTS[variant]
    trail = ma10 if rule["trail_days"] == 10 else ma20
    if holding_day >= spec.trail_start_day and trail is not None and np.isfinite(trail):
        if close < float(trail):
            return "full", f"Q跌破MA{rule['trail_days']}"

    if (rule["partial"] and not partial_done
            and holding_day >= spec.partial_day and close > entry_price):
        return "partial", "Q第5日獲利減半"
    return None, None


def attach_unconditional_entry_paths(
    trades: pd.DataFrame,
    closes: pd.DataFrame,
    horizons: tuple[int, ...] = (3, 5, 10),
) -> pd.DataFrame:
    """Attach post-entry returns even when the simulated trade exited earlier.

    This prevents a subtle survivor bias: day-10 statistics must include stocks
    stopped on day 2, not only positions that happened to survive ten days.
    ``entry_date`` is day one and execution ``entry_price`` is the denominator.
    """
    out = trades.copy()
    dates = list(closes.index)
    locations = {d: i for i, d in enumerate(dates)}
    for horizon in horizons:
        values = []
        offset = int(horizon) - 1
        for row in out.itertuples(index=False):
            loc = locations.get(row.entry_date)
            target_i = None if loc is None else loc + offset
            value = np.nan
            if target_i is not None and target_i < len(dates) and row.stock_id in closes.columns:
                close = closes.at[dates[target_i], row.stock_id]
                if pd.notna(close) and row.entry_price:
                    value = float(close) / float(row.entry_price) - 1.0
            values.append(value)
        out[f"ret_day{horizon}"] = values
    out.attrs.update(trades.attrs)
    return out


def trade_distribution(trades: pd.DataFrame) -> dict:
    """Unconditional path, R-multiple, and right-tail concentration diagnostics."""
    if trades is None or trades.empty:
        return {}
    pnl = pd.to_numeric(trades.get("net_pnl"), errors="coerce").dropna()
    r = pd.to_numeric(trades.get("r_multiple"), errors="coerce").dropna()
    ordered = pnl.sort_values(ascending=False)
    total_positive = float(pnl[pnl > 0].sum())
    result = {
        "trades": int(len(trades)),
        "win_rate": float((pnl > 0).mean()),
        "mean_r": float(r.mean()) if len(r) else None,
        "median_r": float(r.median()) if len(r) else None,
        "r_ge_1": int((r >= 1).sum()),
        "r_ge_3": int((r >= 3).sum()),
        "r_ge_5": int((r >= 5).sum()),
        "r_ge_10": int((r >= 10).sum()),
        "top5_profit_share": (
            float(ordered.head(5).clip(lower=0).sum() / total_positive)
            if total_positive > 0 else None
        ),
        "net_pnl_after_removing_best_5": float(ordered.iloc[5:].sum()),
    }
    for day in (3, 5, 10):
        col = f"ret_day{day}"
        raw = trades[col] if col in trades.columns else pd.Series(dtype=float)
        values = pd.to_numeric(raw, errors="coerce").dropna()
        result[f"day{day}_n"] = int(len(values))
        result[f"day{day}_mean"] = float(values.mean()) if len(values) else None
        result[f"day{day}_median"] = float(values.median()) if len(values) else None
        result[f"day{day}_positive_rate"] = float((values > 0).mean()) if len(values) else None
    return result
