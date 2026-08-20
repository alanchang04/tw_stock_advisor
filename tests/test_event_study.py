"""Phase 2 事件研究引擎的單元測試（合成 fixtures）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.event_study import (
    block_bootstrap_ci,
    forward_excess_returns,
    run_event_study,
)


def panel(n=200, start="2015-01-05", stocks=("1101", "1102")):
    idx = pd.bdate_range(start, periods=n)
    return idx, pd.DataFrame(
        {s: np.full(n, 100.0) for s in stocks}, index=idx)


def flat_benchmark(idx):
    return pd.Series(1000.0, index=idx)


def test_entry_is_next_session_open_not_event_day_close():
    """因子在收盤後才知道，因此進場價必須是 t+1 開盤，不是 t 收盤。"""
    idx, opens = panel()
    opens.iloc[5] = 999.0          # 事件日當天的開盤：不該被用到
    opens.iloc[6] = 100.0          # t+1 開盤：進場價
    opens.iloc[16] = 110.0         # t+1+10 開盤：出場價
    events = pd.DataFrame({"stock_id": ["1101"], "event_date": [idx[5]]})
    out = forward_excess_returns(events=events, open_prices=opens,
                                 benchmark_nav=flat_benchmark(idx), windows=(10,))
    assert out["entry_date"].iloc[0] == idx[6]
    assert out["car_10"].iloc[0] == pytest.approx(0.10)


def test_excess_return_subtracts_the_benchmark():
    idx, opens = panel()
    opens.iloc[6] = 100.0
    opens.iloc[16] = 110.0                      # 個股 +10%
    bench = pd.Series(1000.0, index=idx)
    bench.iloc[16] = 1040.0                     # 基準 +4%
    bench.iloc[6] = 1000.0
    events = pd.DataFrame({"stock_id": ["1101"], "event_date": [idx[5]]})
    out = forward_excess_returns(events=events, open_prices=opens,
                                 benchmark_nav=bench, windows=(10,))
    assert out["car_10"].iloc[0] == pytest.approx(0.06)


def test_event_at_sample_end_is_dropped_not_extrapolated():
    idx, opens = panel(n=30)
    events = pd.DataFrame({"stock_id": ["1101"], "event_date": [idx[-1]]})
    out = forward_excess_returns(events=events, open_prices=opens,
                                 benchmark_nav=flat_benchmark(idx), windows=(5,))
    assert out.empty


def test_window_exceeding_sample_yields_nan_for_that_window_only():
    idx, opens = panel(n=30)
    opens.iloc[6] = 100.0
    opens.iloc[11] = 105.0
    events = pd.DataFrame({"stock_id": ["1101"], "event_date": [idx[5]]})
    out = forward_excess_returns(events=events, open_prices=opens,
                                 benchmark_nav=flat_benchmark(idx), windows=(5, 60))
    assert out["car_5"].iloc[0] == pytest.approx(0.05)
    assert np.isnan(out["car_60"].iloc[0])


def test_unknown_stock_is_skipped():
    idx, opens = panel()
    events = pd.DataFrame({"stock_id": ["9999"], "event_date": [idx[5]]})
    out = forward_excess_returns(events=events, open_prices=opens,
                                 benchmark_nav=flat_benchmark(idx), windows=(5,))
    assert out.empty


def test_missing_required_columns_are_rejected():
    idx, opens = panel()
    with pytest.raises(ValueError, match="stock_id"):
        forward_excess_returns(events=pd.DataFrame({"event_date": [idx[5]]}),
                               open_prices=opens, benchmark_nav=flat_benchmark(idx))


def test_unsorted_price_index_is_rejected():
    idx, opens = panel()
    with pytest.raises(ValueError, match="升冪"):
        forward_excess_returns(events=pd.DataFrame({"stock_id": ["1101"],
                                                    "event_date": [idx[5]]}),
                               open_prices=opens.iloc[::-1],
                               benchmark_nav=flat_benchmark(idx))


def per_event_frame(values, dates):
    return pd.DataFrame({"event_date": pd.to_datetime(dates), "car_10": values})


def test_bootstrap_detects_a_clearly_positive_effect():
    dates = pd.bdate_range("2015-01-05", periods=400)
    frame = per_event_frame(np.full(400, 0.05), dates)
    low, high, p = block_bootstrap_ci(frame, 10, sessions=dates, draws=300)
    assert low > 0
    assert p < 0.05


def test_bootstrap_does_not_flag_pure_noise():
    rng = np.random.default_rng(1)
    dates = pd.bdate_range("2015-01-05", periods=600)
    frame = per_event_frame(rng.normal(0, 0.05, 600), dates)
    low, high, p = block_bootstrap_ci(frame, 10, sessions=dates, draws=300)
    assert low < 0 < high
    assert p > 0.05


def test_bootstrap_is_deterministic_for_a_fixed_seed():
    dates = pd.bdate_range("2015-01-05", periods=200)
    frame = per_event_frame(np.linspace(-0.02, 0.03, 200), dates)
    a = block_bootstrap_ci(frame, 10, sessions=dates, draws=200, seed=7)
    b = block_bootstrap_ci(frame, 10, sessions=dates, draws=200, seed=7)
    assert a == b


def test_clustered_events_widen_the_interval_versus_spread_events():
    """同期事件高度相關，block 重抽必須反映出較低的有效樣本數。"""
    values = np.full(300, 0.01)
    calendar = pd.bdate_range("2015-01-05", periods=300)
    clustered = per_event_frame(values, calendar)
    same_day = per_event_frame(values, [pd.Timestamp("2015-01-05")] * 300)
    _, _, p_spread = block_bootstrap_ci(clustered, 10, sessions=calendar, draws=300)
    low_c, high_c, _ = block_bootstrap_ci(same_day, 10, sessions=calendar, draws=300)
    # 全部集中在同一天 → 只有一個 block → 重抽後區間退化為單點
    assert low_c == high_c


def test_run_event_study_reports_every_window():
    idx, opens = panel(n=300)
    opens.iloc[6] = 100.0
    for offset, price in ((11, 101.0), (16, 102.0), (26, 103.0),
                          (46, 104.0), (66, 105.0)):
        opens.iloc[offset] = price
    events = pd.DataFrame({"stock_id": ["1101"], "event_date": [idx[5]]})
    result = run_event_study(events=events, open_prices=opens,
                             benchmark_nav=flat_benchmark(idx), draws=50)
    assert set(result.car_mean) == {5, 10, 20, 40, 60}
    assert result.events == 1
    assert result.is_monotonic_path()
