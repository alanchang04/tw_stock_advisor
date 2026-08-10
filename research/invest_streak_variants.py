"""P3-11 alternative definitions of the 投信 accumulation streak.

The production definition is brittle: a day is a "buy day" iff `invest_net > 0`,
with no size floor, and any negative day resets the count.  Measured on
development/TWSE, **61.9% of established streaks are broken by a sell of 20 lots
or less**, and the median breaking sell is 1% of that streak's own average daily
buy.  `invest_net` is the aggregate across all trust funds, so one fund trimming
can flip the sign while the overall stance is still accumulation.

Both alternatives keep the production output shape (date x stock streak length,
zeroed unless the accumulated size clears the floor) so they can be swapped
straight into `data["_inv_streak"]`.

Thresholds come from the measured distribution, not from another context -- the
mistake that cost P3-10.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from agent.strategy import MIN_INVEST_STREAK_LOTS

#: 單日淨賣 ≤ 該段目前平均買超的此比例時，不打斷 streak（實測涵蓋 61.5% 的中斷）
TOLERANCE_RATIO = 0.10
#: 單日買超需達當日成交量的此比例才算「買超日」（實測保留 32.9% 的買超日）
SIZE_FLOOR_PCT_OF_VOLUME = 0.05


def _empty_like(frame: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(0.0, index=frame.index, columns=frame.columns)


def streak_with_tolerance(invest_lots: pd.DataFrame,
                          tolerance_ratio: float = TOLERANCE_RATIO,
                          min_lots: float = MIN_INVEST_STREAK_LOTS) -> pd.DataFrame:
    """A small net sell no longer resets the count.

    "Small" is relative to the streak's own average buy, so it adapts to the stock
    rather than importing an absolute lot figure.  The size floor switches from
    cumulative *buys* to cumulative *net*: with tolerance alone, a pattern of
    "buy 100, sell 10" would extend a streak forever, and counting net is what
    stops that -- part of the same mechanism, not a second knob.
    """
    out = _empty_like(invest_lots)
    arr = invest_lots.to_numpy(dtype=float)
    res = np.zeros_like(arr)

    for j in range(arr.shape[1]):
        col = arr[:, j]
        run_len = 0          # 買超日數（被容忍的賣出日不計入）
        buy_sum = 0.0        # 只累計買超，用來算「該段平均買超」
        net_sum = 0.0        # 淨額，含被容忍的賣出——量體門檻看這個
        for i in range(col.shape[0]):
            v = col[i]
            if np.isnan(v):
                res[i, j] = 0.0
                continue
            if v > 0:
                run_len += 1
                buy_sum += v
                net_sum += v
            elif run_len > 0 and abs(v) <= tolerance_ratio * (buy_sum / run_len):
                net_sum += v          # 容忍：不重置，但這天不算一個買超日
            else:
                run_len, buy_sum, net_sum = 0, 0.0, 0.0
            res[i, j] = run_len if (run_len > 0 and net_sum >= min_lots) else 0.0

    out.iloc[:, :] = res
    return out


def streak_with_size_floor(invest_lots: pd.DataFrame, volume_lots: pd.DataFrame,
                           floor_pct: float = SIZE_FLOOR_PCT_OF_VOLUME,
                           min_lots: float = MIN_INVEST_STREAK_LOTS) -> pd.DataFrame:
    """A day only counts when the buy is large relative to that day's volume.

    Using a share of volume rather than an absolute lot count keeps the test
    meaningful across stock sizes -- 50 lots is noise in 2330 and material in a
    small cap.  Anything below the floor breaks the streak, matching the existing
    semantics where a non-buy day resets it.
    """
    vol = volume_lots.reindex(index=invest_lots.index, columns=invest_lots.columns)
    out = _empty_like(invest_lots)
    inv = invest_lots.to_numpy(dtype=float)
    vv = vol.to_numpy(dtype=float)
    res = np.zeros_like(inv)

    for j in range(inv.shape[1]):
        run_len, buy_sum = 0, 0.0
        for i in range(inv.shape[0]):
            v, vol_i = inv[i, j], vv[i, j]
            qualifies = (not np.isnan(v) and not np.isnan(vol_i) and vol_i > 0
                         and v > 0 and v >= floor_pct * vol_i)
            if qualifies:
                run_len += 1
                buy_sum += v
            else:
                run_len, buy_sum = 0, 0.0
            res[i, j] = run_len if (run_len > 0 and buy_sum >= min_lots) else 0.0

    out.iloc[:, :] = res
    return out


def qualifying_stock_days(streak: pd.DataFrame) -> int:
    """合格 streak 的股票日數——F0b 處置檢查用（介入有沒有真的生效）。"""
    return int((streak > 0).to_numpy().sum())
