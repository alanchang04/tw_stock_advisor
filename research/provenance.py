"""Environment and data provenance for research reports.

A backtest number is only meaningful alongside the environment that produced it.
Measured on 2026-08-06 with identical code, data and window:

    pandas 2.2.3 -> 15.22% / Sharpe 0.91 / MDD -21.28% / 237 trades
    pandas 3.0.5 -> 14.07% / Sharpe 0.85 / MDD -24.28% / 242 trades

Without this block, a failed reproduction cannot be distinguished from a code
regression -- which is exactly what happened to the P3-9 F0 check, costing an
hour of bisecting before the cause turned out to be the environment.

Attach :func:`environment_block` to every research payload.
"""
from __future__ import annotations

import platform
import sys

import pandas as pd


def _version(module_name: str) -> str | None:
    try:
        module = __import__(module_name)
    except ImportError:
        return None
    return getattr(module, "__version__", None)


def data_fingerprint(data: dict | None) -> dict:
    """Summarise the loaded dataset so another machine can tell if it differs."""
    if not data:
        return {}
    prices = data.get("prices")
    if prices is None or getattr(prices, "empty", True):
        return {}
    per_day = prices.groupby("trade_date")["stock_id"].nunique()
    dividends = data.get("dividends")
    return {
        "price_rows": int(len(prices)),
        "price_stocks": int(prices["stock_id"].nunique()),
        "price_days": int(prices["trade_date"].nunique()),
        "price_first": str(prices["trade_date"].min()),
        # The last date matters: total-return adjustment is applied backwards, so a
        # longer dataset shifts historical adjusted price levels (small but real).
        "price_last": str(prices["trade_date"].max()),
        "median_stocks_per_day": float(per_day.median()) if len(per_day) else 0.0,
        "dividend_events": (
            0 if dividends is None or getattr(dividends, "empty", True)
            else int(len(dividends))
        ),
    }


def environment_block(data: dict | None = None, parquet_dir: str | None = None) -> dict:
    """Versions plus a data fingerprint.  Cheap; attach it to every report."""
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "pandas": pd.__version__,
        "numpy": _version("numpy"),
        "scipy": _version("scipy"),
        "parquet_dir": str(parquet_dir) if parquet_dir else None,
        "data": data_fingerprint(data),
        "note": (
            "回測數字只在這組環境下可複製；pandas 版本會改變選股結果，"
            "比對他人數字前先核對本區塊（見 requirements.txt 的鎖版註解）"
        ),
    }
