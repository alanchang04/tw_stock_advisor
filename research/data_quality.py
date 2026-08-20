"""Acceptance gates for historical backtest datasets."""
from __future__ import annotations

import pandas as pd


def backtest_data_quality(data: dict, min_coverage: float = .80) -> dict:
    prices = data.get("prices")
    if prices is None or prices.empty:
        return {"passed": False, "coverage": 0.0, "errors": ["prices empty"]}
    dates = pd.to_datetime(prices["trade_date"], errors="coerce").dropna()
    grid = pd.bdate_range(dates.min(), dates.max()) if not dates.empty else []
    coverage = dates.dt.normalize().nunique() / max(1, len(grid))
    errors = []
    if coverage < min_coverage:
        errors.append(f"date coverage {coverage:.1%} < {min_coverage:.0%}")
    duplicate_rows = int(prices.duplicated(["stock_id", "trade_date"]).sum())
    if duplicate_rows:
        errors.append(f"duplicate price keys: {duplicate_rows}")
    rows_per_day = prices.groupby("trade_date")["stock_id"].nunique()
    median_rows = float(rows_per_day.median()) if len(rows_per_day) else 0.0
    sparse_days = int((rows_per_day < median_rows * .5).sum()) if median_rows else 0
    if sparse_days:
        errors.append(f"suspicious partial-market days: {sparse_days}")
    benchmark_rows = prices[prices["stock_id"].astype(str).eq("0050")]
    benchmark_coverage = (benchmark_rows["trade_date"].nunique() /
                          max(1, dates.dt.normalize().nunique()))
    if benchmark_coverage < min_coverage:
        errors.append(f"0050 coverage {benchmark_coverage:.1%} < {min_coverage:.0%}")
    ohlcv_missing = float(prices.reindex(columns=["open", "high", "low", "close", "volume"])
                          .isna().any(axis=1).mean())
    if ohlcv_missing > .01:
        errors.append(f"OHLCV missing rows {ohlcv_missing:.1%} > 1%")
    inst = data.get("inst")
    inst_dates = 0 if inst is None or inst.empty else inst["trade_date"].nunique()
    institutional_coverage = inst_dates / max(1, dates.dt.normalize().nunique())
    if institutional_coverage < .60:
        errors.append(f"institutional date coverage {institutional_coverage:.1%} < 60%")
    return {"passed": not errors, "coverage": coverage,
            "benchmark_coverage": benchmark_coverage,
            "institutional_coverage": institutional_coverage,
            "median_stocks_per_day": median_rows, "sparse_days": sparse_days,
            "duplicate_rows": duplicate_rows, "ohlcv_missing": ohlcv_missing,
            "errors": errors}
