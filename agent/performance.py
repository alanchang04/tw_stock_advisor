"""Portfolio and benchmark performance utilities with consistent cash accounting."""
from __future__ import annotations

import math
import pandas as pd

from agent.strategy import FEE_RATE, TAX_RATE, SLIPPAGE


def buy_and_hold_nav(prices: pd.Series, initial_capital: float,
                     fee_rate: float = FEE_RATE, tax_rate: float = TAX_RATE,
                     slippage: float = SLIPPAGE) -> pd.Series:
    """Integer-share buy-and-hold NAV, including entry/exit friction.

    ``prices`` must already be a point-in-time total-return-adjusted series when
    dividends/splits are intended to be included.
    """
    px = pd.to_numeric(prices, errors="coerce").dropna()
    px = px[px > 0].sort_index()
    if px.empty or initial_capital <= 0:
        return pd.Series(dtype=float)
    entry = float(px.iloc[0]) * (1 + slippage)
    shares = int(float(initial_capital) // (entry * (1 + fee_rate)))
    if shares <= 0:
        return pd.Series(float(initial_capital), index=px.index, dtype=float)
    cash = float(initial_capital) - shares * entry * (1 + fee_rate)
    nav = cash + shares * px.astype(float)
    exit_fill = float(px.iloc[-1]) * (1 - slippage)
    nav.iloc[-1] = cash + shares * exit_fill * (1 - fee_rate - tax_rate)
    return nav


def compound_trade_returns(returns, initial: float = 1.0) -> pd.Series:
    """Compound sequential returns; retained only for non-overlapping trade reports."""
    r = pd.to_numeric(pd.Series(returns), errors="coerce").fillna(0.0)
    return float(initial) * (1.0 + r).cumprod()


def metric_table(strategy_metrics: dict, benchmark_metrics: dict | None) -> pd.DataFrame:
    rows = {"strategy": strategy_metrics}
    if benchmark_metrics:
        rows["0050"] = benchmark_metrics
    keys = ["total", "ann_ret", "ann_vol", "sharpe", "mdd", "calmar",
            "coverage", "observations", "calendar_years"]
    return pd.DataFrame({name: {k: values.get(k, math.nan) for k in keys}
                         for name, values in rows.items()}).T
