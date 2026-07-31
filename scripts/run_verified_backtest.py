"""Run the swing backtest from the frozen local snapshot and save reproducible metrics."""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.backtest import run_backtest


def _yearly(nav: dict) -> dict[str, float]:
    series = pd.Series(nav, dtype=float)
    series.index = pd.to_datetime(series.index)
    year_end = series.resample("YE").last()
    previous = float(series.iloc[0])
    result = {}
    for stamp, value in year_end.items():
        result[str(stamp.year)] = float(value / previous - 1)
        previous = float(value)
    return result


def _jsonable(value):
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def main():
    trades = run_backtest(parquet_dir="data/research", quiet=True)
    attrs = dict(trades.attrs)
    nav = attrs.pop("nav")
    nav_0050 = attrs.pop("nav_0050")
    report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "snapshot_dir": os.path.abspath("data/research"),
        "trades": len(trades),
        "metrics": attrs,
        "yearly_returns": _yearly(nav),
        "yearly_returns_0050": _yearly(nav_0050) if nav_0050 else {},
    }
    os.makedirs("reports", exist_ok=True)
    with open("reports/swing_backtest_verified.json", "w", encoding="utf-8") as fh:
        json.dump(_jsonable(report), fh, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps(_jsonable(report), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
