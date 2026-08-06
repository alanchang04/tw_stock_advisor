"""P3-9 breakout-timing features for the production quality ranking.

Deliberately separate from :mod:`research.qullamaggie`.  That module *selects*
stocks (prior move, relative strength, ADR, volume expansion).  This one selects
nothing: it takes a list the production revenue/institutional ranking already
chose and only answers *when* to buy it.  Keeping the two apart is what makes the
P3-9 result attributable to entry timing rather than to a second stock screen.

Nothing here is shifted.  Every value uses data up to and including day T, which
is known once T has closed; the portfolio runner places the order after that
close and executes it during T+1.  A trigger built from ``high`` through T is
therefore a genuine buy-stop, not lookahead.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd


@dataclass(frozen=True)
class QualityTimingSpec:
    """Thresholds fixed in research/EXPERIMENTS.md before P3-9 was run.

    They are pre-registered.  Do not tune them against results, and do not scan
    neighbouring values looking for a winner (see the P3-5 lesson).
    """

    box_days: int = 20
    tight_days: int = 10
    dryup_days: int = 5
    max_distance: float = 0.03
    tightness_max: float = 0.75

    def to_dict(self) -> dict:
        return asdict(self)


def _at(panel: pd.DataFrame, d, sid) -> float | None:
    try:
        value = panel.at[d, sid]
    except KeyError:
        return None
    return None if pd.isna(value) else float(value)


def build_quality_box_panels(
    highs: pd.DataFrame,
    lows: pd.DataFrame,
    closes: pd.DataFrame,
    volumes: pd.DataFrame,
    spec: QualityTimingSpec = QualityTimingSpec(),
) -> dict[str, pd.DataFrame]:
    """Box trigger, tightness ratio and dry-up features known at each close.

    ``tightness_ratio`` compares the recent 10-day range against the *same*
    trailing 20-day window that produces the trigger.  EXPERIMENTS.md records this
    reading of the spec's ambiguous wording, so the experiment never carries two
    different 20-day definitions at once.
    """
    highs, lows, volumes = (
        p.reindex(index=closes.index, columns=closes.columns)
        for p in (highs, lows, volumes)
    )
    box = spec.box_days

    trigger = highs.rolling(box, min_periods=box).max()
    box_low = lows.rolling(box, min_periods=box).min()
    box_range = (trigger - box_low) / trigger.where(trigger > 0)

    tight_high = highs.rolling(spec.tight_days, min_periods=spec.tight_days).max()
    tight_low = lows.rolling(spec.tight_days, min_periods=spec.tight_days).min()
    tight_range = (tight_high - tight_low) / tight_high.where(tight_high > 0)
    tightness_ratio = tight_range / box_range.where(box_range > 0)

    # trigger >= high_T >= close_T, so distance is non-negative by construction.
    distance = trigger / closes.where(closes > 0) - 1.0

    vol_recent = volumes.rolling(spec.dryup_days, min_periods=spec.dryup_days).mean()
    vol_base = volumes.rolling(box, min_periods=box).mean()

    return {
        "trigger": trigger,
        "box_range": box_range,
        "tight_range": tight_range,
        "tightness_ratio": tightness_ratio,
        "distance": distance,
        "vol_recent": vol_recent,
        "vol_base": vol_base,
    }


def select_quality_stop_buy_orders(
    ranked: list[str],
    boxes: dict[str, pd.DataFrame],
    d,
    spec: QualityTimingSpec = QualityTimingSpec(),
    *,
    require_tight: bool = False,
    require_dryup: bool = False,
) -> list[dict]:
    """Reduce a ranked quality list to orders that can actually be placed.

    Quality rank order is preserved.  This stage only removes names whose chart is
    not at a placeable breakout point; it never re-sorts, because re-sorting would
    quietly turn the timing filter into a competing ranking.
    """
    orders: list[dict] = []
    for sid in ranked:
        trigger = _at(boxes["trigger"], d, sid)
        distance = _at(boxes["distance"], d, sid)
        if trigger is None or distance is None:
            continue
        if distance < 0.0 or distance > spec.max_distance:
            continue
        if require_tight:
            ratio = _at(boxes["tightness_ratio"], d, sid)
            if ratio is None or ratio > spec.tightness_max:
                continue
        if require_dryup:
            recent = _at(boxes["vol_recent"], d, sid)
            base = _at(boxes["vol_base"], d, sid)
            if recent is None or base is None or recent >= base:
                continue
        orders.append({"stock_id": str(sid), "trigger": trigger})
    return orders
