"""P3-9 quality breakout-timing features.

The point-in-time test matters most: a trigger that silently used tomorrow's high
would make the whole study lookahead, and the resulting numbers would look better
rather than obviously broken.
"""
import datetime as dt

import pandas as pd
import pytest

from research.quality_timing import (
    QualityTimingSpec,
    build_quality_box_panels,
    select_quality_stop_buy_orders,
)


def _frame(values: dict[str, list[float]], days: int) -> pd.DataFrame:
    index = [dt.date(2020, 1, 1) + dt.timedelta(days=i) for i in range(days)]
    return pd.DataFrame(values, index=index)


@pytest.fixture
def panels():
    days = 25
    # A flat base at 100 with a single spike to 130 on the final day.
    highs = [100.0] * (days - 1) + [130.0]
    lows = [90.0] * days
    closes = [95.0] * (days - 1) + [128.0]
    volumes = [1000.0] * days
    return {
        "highs": _frame({"A": highs}, days),
        "lows": _frame({"A": lows}, days),
        "closes": _frame({"A": closes}, days),
        "volumes": _frame({"A": volumes}, days),
    }


def test_trigger_uses_only_data_up_to_and_including_today(panels):
    boxes = build_quality_box_panels(
        panels["highs"], panels["lows"], panels["closes"], panels["volumes"]
    )
    index = list(panels["closes"].index)
    # The day before the spike may not already know about the 130 high.
    assert boxes["trigger"].at[index[-2], "A"] == 100.0
    # On the spike day itself the high is known after the close, so it is usable.
    assert boxes["trigger"].at[index[-1], "A"] == 130.0


def test_distance_is_never_negative(panels):
    boxes = build_quality_box_panels(
        panels["highs"], panels["lows"], panels["closes"], panels["volumes"]
    )
    distance = boxes["distance"]["A"].dropna()
    assert len(distance) > 0
    assert (distance >= 0).all()


def test_box_windows_need_full_history_before_emitting(panels):
    boxes = build_quality_box_panels(
        panels["highs"], panels["lows"], panels["closes"], panels["volumes"]
    )
    index = list(panels["closes"].index)
    spec = QualityTimingSpec()
    # A 20-day box cannot exist on day 19.
    assert pd.isna(boxes["trigger"].at[index[spec.box_days - 2], "A"])
    assert not pd.isna(boxes["trigger"].at[index[spec.box_days - 1], "A"])


def test_distance_gate_rejects_names_far_below_the_box_top():
    days = 25
    highs = _frame({"NEAR": [100.0] * days, "FAR": [100.0] * days}, days)
    lows = _frame({"NEAR": [90.0] * days, "FAR": [90.0] * days}, days)
    # NEAR closes 1% under the box top, FAR closes 20% under it.
    closes = _frame({"NEAR": [99.0] * days, "FAR": [80.0] * days}, days)
    volumes = _frame({"NEAR": [1000.0] * days, "FAR": [1000.0] * days}, days)
    boxes = build_quality_box_panels(highs, lows, closes, volumes)
    d = list(closes.index)[-1]

    orders = select_quality_stop_buy_orders(["NEAR", "FAR"], boxes, d)

    assert [o["stock_id"] for o in orders] == ["NEAR"]
    assert orders[0]["trigger"] == 100.0


def test_selection_preserves_quality_rank_order():
    days = 25
    ids = ["C", "A", "B"]
    highs = _frame({s: [100.0] * days for s in ids}, days)
    lows = _frame({s: [90.0] * days for s in ids}, days)
    closes = _frame({s: [99.0] * days for s in ids}, days)
    volumes = _frame({s: [1000.0] * days for s in ids}, days)
    boxes = build_quality_box_panels(highs, lows, closes, volumes)
    d = list(closes.index)[-1]

    orders = select_quality_stop_buy_orders(ids, boxes, d)

    assert [o["stock_id"] for o in orders] == ids


def test_tightness_gate_rejects_a_wide_recent_range():
    days = 25
    # Both sit just under the same box top, so only tightness can separate them:
    # TIGHT coils into a 3%-deep range over the last 10 days, WIDE keeps swinging
    # the full 20% box.
    highs = _frame({"TIGHT": [100.0] * days, "WIDE": [100.0] * days}, days)
    lows = _frame({"TIGHT": [80.0] * 15 + [97.0] * 10, "WIDE": [80.0] * days}, days)
    closes = _frame({"TIGHT": [99.0] * days, "WIDE": [99.0] * days}, days)
    volumes = _frame({"TIGHT": [1000.0] * days, "WIDE": [1000.0] * days}, days)
    boxes = build_quality_box_panels(highs, lows, closes, volumes)
    d = list(closes.index)[-1]

    both = select_quality_stop_buy_orders(["TIGHT", "WIDE"], boxes, d)
    tightened = select_quality_stop_buy_orders(
        ["TIGHT", "WIDE"], boxes, d, require_tight=True
    )

    assert {o["stock_id"] for o in both} == {"TIGHT", "WIDE"}
    assert [o["stock_id"] for o in tightened] == ["TIGHT"]


def test_dryup_gate_rejects_rising_recent_volume():
    days = 25
    highs = _frame({"DRY": [100.0] * days, "WET": [100.0] * days}, days)
    lows = _frame({"DRY": [90.0] * days, "WET": [90.0] * days}, days)
    closes = _frame({"DRY": [99.0] * days, "WET": [99.0] * days}, days)
    volumes = _frame(
        {
            "DRY": [1000.0] * 20 + [500.0] * 5,
            "WET": [1000.0] * 20 + [3000.0] * 5,
        },
        days,
    )
    boxes = build_quality_box_panels(highs, lows, closes, volumes)
    d = list(closes.index)[-1]

    orders = select_quality_stop_buy_orders(
        ["DRY", "WET"], boxes, d, require_dryup=True
    )

    assert [o["stock_id"] for o in orders] == ["DRY"]


def test_gates_are_cumulative_so_h3_is_a_subset_of_h1():
    days = 25
    highs = _frame({"A": [100.0] * days}, days)
    lows = _frame({"A": [90.0] * days}, days)
    closes = _frame({"A": [99.0] * days}, days)
    # Volume rises at the end, so the dry-up gate must remove the only candidate.
    volumes = _frame({"A": [1000.0] * 20 + [5000.0] * 5}, days)
    boxes = build_quality_box_panels(highs, lows, closes, volumes)
    d = list(closes.index)[-1]

    h1 = select_quality_stop_buy_orders(["A"], boxes, d)
    h3 = select_quality_stop_buy_orders(
        ["A"], boxes, d, require_tight=True, require_dryup=True
    )

    assert [o["stock_id"] for o in h1] == ["A"]
    assert h3 == []
