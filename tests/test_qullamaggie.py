import numpy as np
import pandas as pd
import pytest

from research.qullamaggie import (
    QullamaggieSpec,
    attach_unconditional_entry_paths,
    assume_same_day_stop,
    build_breakout_features,
    build_prior_day_watchlist,
    initial_q_stop,
    q_exit_decision,
    stop_buy_fill,
    trade_distribution,
)


def _breakout_panels():
    dates = pd.bdate_range("2020-01-01", periods=150).date
    # A 40% prior run, followed by a 20-day orderly base and a final breakout.
    close = np.r_[
        np.full(70, 100.0), np.linspace(100, 140, 60),
        np.linspace(136, 142, 19), [150.0],
    ]
    frame = pd.DataFrame({"X": close}, index=dates)
    opens = frame * 0.99
    highs = frame * 1.03
    lows = frame * 0.97
    highs.iloc[-1, 0] = 151.0
    lows.iloc[-1, 0] = 145.0
    volumes = pd.DataFrame(1_000.0, index=dates, columns=["X"])
    volumes.iloc[-1, 0] = 2_000.0
    return opens, highs, lows, frame, volumes


def test_breakout_signal_uses_only_information_available_at_signal_close():
    panels = _breakout_panels()
    features = build_breakout_features(*panels)
    signal_day = panels[3].index[-1]
    assert bool(features["signal"].at[signal_day, "X"])

    # Adding a spectacular future bar cannot alter the already-computed signal.
    future = pd.Timestamp(signal_day) + pd.offsets.BDay(1)
    extended = []
    for panel in panels:
        value = panel.iloc[-1] * 5
        extended.append(pd.concat([panel, pd.DataFrame([value], index=[future.date()])]))
    after = build_breakout_features(*extended)
    assert after["signal"].at[signal_day, "X"] == features["signal"].at[signal_day, "X"]


def test_initial_stop_is_signal_low_but_never_wider_than_one_adr():
    spec = QullamaggieSpec()
    assert initial_q_stop(100, 97, 0.05, spec) == 97
    assert initial_q_stop(100, 80, 0.05, spec) == 95
    # Gap below the signal low must not create a stop above entry.
    assert initial_q_stop(90, 97, 0.05, spec) == 85.5


def test_partial_profit_precedes_break_even_move_and_requires_profit():
    args = dict(entry_price=100, stop_price=95, holding_day=5,
                ma10=98, ma20=96, partial_done=False,
                variant="q_half_day5_10ma")
    assert q_exit_decision(close=104, **args) == ("partial", "Q第5日獲利減半")
    assert q_exit_decision(close=99, **args) == (None, None)


def test_full_risk_exit_has_priority_over_partial():
    action, reason = q_exit_decision(
        close=94, entry_price=100, stop_price=95, holding_day=5,
        ma10=90, ma20=89, partial_done=False, variant="q_half_day5_10ma",
    )
    assert action == "full"
    assert "停損" in reason


def test_trade_distribution_is_unconditional_and_reports_tail_concentration():
    trades = pd.DataFrame({
        "net_pnl": [100, -10, 20, -5, 50, 200],
        "r_multiple": [2, -1, .5, -.5, 3, 10],
        "ret_day3": [.1, -.1, .02, -.03, .2, .4],
        "ret_day5": [.2, -.2, .01, -.04, .3, .8],
    })
    result = trade_distribution(trades)
    assert result["trades"] == 6
    assert result["day3_n"] == 6
    assert result["day3_positive_rate"] == 4 / 6
    assert result["r_ge_3"] == 2
    assert result["top5_profit_share"] == 1.0


def test_entry_path_keeps_following_stock_after_early_exit():
    dates = pd.bdate_range("2024-01-01", periods=10).date
    closes = pd.DataFrame({"X": range(100, 110)}, index=dates)
    trades = pd.DataFrame([{
        "stock_id": "X", "entry_date": dates[0], "entry_price": 100.0,
        "exit_date": dates[1], "net_pnl": -1.0, "r_multiple": -1.0,
    }])
    enriched = attach_unconditional_entry_paths(trades, closes)
    assert enriched.loc[0, "ret_day3"] == pytest.approx(0.02)
    assert enriched.loc[0, "ret_day5"] == pytest.approx(0.04)
    assert enriched.loc[0, "ret_day10"] == pytest.approx(0.09)


def test_preplaced_stop_buy_only_fills_when_trigger_is_touched_and_respects_gaps():
    assert stop_buy_fill(98, 99, 100, .003) is None
    assert stop_buy_fill(98, 101, 100, .003) == pytest.approx(100.3)
    assert stop_buy_fill(105, 108, 100, .003) == pytest.approx(105.315)


def test_prior_day_watchlist_is_fixed_before_trigger_day():
    panels = _breakout_panels()
    watch = build_prior_day_watchlist(*panels)
    watch_day = panels[3].index[-2]
    assert bool(watch["watch"].at[watch_day, "X"])
    trigger = watch["trigger"].at[watch_day, "X"]
    # Tomorrow's OHLCV can change without altering tonight's order level.
    changed = [p.copy() for p in panels]
    for panel in changed:
        panel.iloc[-1, 0] *= 3
    after = build_prior_day_watchlist(*changed)
    assert after["watch"].at[watch_day, "X"] == watch["watch"].at[watch_day, "X"]
    assert after["trigger"].at[watch_day, "X"] == trigger


def test_daily_bar_same_day_stop_is_reported_as_bounds():
    # Open below trigger: the low may have occurred before the breakout.
    assert assume_same_day_stop(day_open=98, trigger=100, day_low=94,
                                stop_price=95, path_assumption="conservative")
    assert not assume_same_day_stop(day_open=98, trigger=100, day_low=94,
                                    stop_price=95, path_assumption="relaxed")
    # Gap through trigger: the position exists at open, so the low is actionable.
    assert assume_same_day_stop(day_open=102, trigger=100, day_low=94,
                                stop_price=95, path_assumption="relaxed")
