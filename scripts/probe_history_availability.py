"""How far back does each data source actually go?

The strategy's factor weights need four data types simultaneously: prices,
per-stock institutional flows (投信 2.5 + 投信新進場 2.5 + 外資 1.0 = 6.0 of the
weight), monthly revenue (3.0 + 1.0) and dividend events (for total-return
adjustment).  Extending `historical_backfill_local.py --start-year` past the
earliest year that has *all four* is wasted effort: the factors simply cannot be
computed, so those years would silently contribute nothing.

Run this before committing to a multi-day backfill.

    python scripts/probe_history_availability.py
    python scripts/probe_history_availability.py --years 2010 2012 2014

Read-only: fetches a handful of public pages, writes nothing, touches no DB.
An empty result means "this endpoint returned no rows for that date" -- which can
also mean a non-trading day or throttling, so each year is retried across several
candidate dates before being called unavailable.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from data_pipeline.fetchers.twse_fetcher import (
    fetch_institutional_tpex_by_date,
    fetch_institutional_twse,
    fetch_prices_tpex_by_date,
    fetch_prices_twse_by_date,
)
from data_pipeline.fetchers.revenue_fetcher import fetch_month_rows

DEFAULT_YEARS = (2005, 2008, 2010, 2012, 2013, 2014, 2015)
#: Mid-March weekdays; at least one is a trading day in any year.
CANDIDATE_DAYS = (12, 13, 14, 15, 16, 17, 18, 19, 20)
DELAY = 1.5


def _rows(fn, *args) -> int | None:
    """Row count, or None when the call itself failed (not the same as empty)."""
    try:
        out = fn(*args)
    except Exception as exc:  # noqa: BLE001 - a probe must never abort the sweep
        print(f"      ! {type(exc).__name__}: {str(exc)[:90]}")
        return None
    time.sleep(DELAY)
    try:
        return len(out)
    except TypeError:
        return None


def _find_trading_day(year: int) -> tuple[date | None, int]:
    """First mid-March date whose TWSE price feed returns rows."""
    for day in CANDIDATE_DAYS:
        d = date(year, 3, day)
        if d.weekday() >= 5:
            continue
        n = _rows(fetch_prices_twse_by_date, d)
        if n:
            return d, n
    return None, 0


def probe(year: int) -> dict:
    print(f"\n=== {year} ===")
    trading_day, twse_price_rows = _find_trading_day(year)
    if trading_day is None:
        print("  prices_twse           UNAVAILABLE (no mid-March date returned rows)")
        return {"year": year, "trading_day": None, "prices_twse": 0, "prices_tpex": 0,
                "inst_twse": 0, "inst_tpex": 0, "revenue": 0}
    print(f"  probe date            {trading_day}")
    print(f"  prices_twse           {twse_price_rows}")

    result = {"year": year, "trading_day": str(trading_day),
              "prices_twse": twse_price_rows}
    for label, fn in (("prices_tpex", fetch_prices_tpex_by_date),
                      ("inst_twse", fetch_institutional_twse),
                      ("inst_tpex", fetch_institutional_tpex_by_date)):
        n = _rows(fn, trading_day)
        result[label] = n or 0
        print(f"  {label:21s} {'FAILED' if n is None else n}")

    rev = _rows(fetch_month_rows, year, 3)
    result["revenue"] = rev or 0
    print(f"  {'revenue(3月)':21s} {'FAILED' if rev is None else rev}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, nargs="+", default=list(DEFAULT_YEARS))
    args = parser.parse_args()

    print("探測各資料源可回溯年份（唯讀，只打公開端點）")
    print(f"年份：{args.years}｜每次請求間隔 {DELAY}s")

    results = [probe(y) for y in sorted(args.years)]

    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    header = f"{'year':>6s} {'px_twse':>9s} {'px_tpex':>9s} {'inst_twse':>10s} {'inst_tpex':>10s} {'revenue':>8s}"
    print(header)
    for r in results:
        print(f"{r['year']:6d} {r['prices_twse']:9d} {r['prices_tpex']:9d} "
              f"{r['inst_twse']:10d} {r['inst_tpex']:10d} {r['revenue']:8d}")

    # The usable start year is the earliest year where *every* source has rows --
    # a year with prices but no institutional data cannot produce the 6.0 of factor
    # weight that depends on 投信/外資, so it would enter the backtest as dead space.
    usable = [r["year"] for r in results
              if all(r[k] > 0 for k in ("prices_twse", "prices_tpex",
                                        "inst_twse", "inst_tpex", "revenue"))]
    print("\n可用年份（五項全有資料）：", usable or "無")
    if usable:
        print(f"→ 建議 historical_backfill_local.py --start-year {min(usable)}")
        blocked = [r for r in results if r["year"] < min(usable)]
        for r in blocked:
            missing = [k for k in ("prices_twse", "prices_tpex", "inst_twse",
                                   "inst_tpex", "revenue") if r[k] == 0]
            print(f"   {r['year']} 不可用，缺：{', '.join(missing)}")
    print("\n⚠️ 「0」可能是端點限流或非交易日，不必然代表該年無資料。"
          "若某年只差一項，建議手動複驗該項再下結論。")


if __name__ == "__main__":
    main()
