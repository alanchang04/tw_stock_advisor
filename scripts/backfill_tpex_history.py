"""Archive and normalize official TPEx history for D5 (2008-2014 by default)."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
import uuid

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.tpex_history import (  # noqa: E402
    parse_attention,
    parse_daily_quotes,
    parse_delisted,
    parse_disposal,
    parse_ex_daily,
    parse_legacy_altered_html,
    parse_trading_restrictions,
)


BASE = "https://www.tpex.org.tw/www/zh-tw"
ENDPOINTS = {
    "daily_quotes": f"{BASE}/afterTrading/dailyQuotes",
    "corporate_actions": f"{BASE}/bulletin/exDailyQ",
    "delisted": f"{BASE}/company/deListed",
    "attention": f"{BASE}/bulletin/attention",
    "disposal": f"{BASE}/bulletin/disposal",
    "institutional_probe": f"{BASE}/insti/dailyTrade",
    "trading_restrictions": f"{BASE}/afterTrading/chtm",
}
LEGACY_ALTERED_BASE = "https://hist.tpex.org.tw/Hist/STOCK/AFTERTRADING/CMODE"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def write_payload(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        temporary.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_payload(path: Path) -> dict:
    return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


def write_raw_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def fetch_legacy_html(day: date, archive: Path, *, cached_only: bool, delay: float) -> bytes:
    if archive.exists():
        return gzip.decompress(archive.read_bytes())
    if cached_only:
        raise FileNotFoundError(archive)
    roc_key = f"{day.year - 1911:02d}{day:%m%d}"
    request = urllib.request.Request(
        f"{LEGACY_ALTERED_BASE}/CHTM_{roc_key}.HTML",
        headers={"User-Agent": "tw-stock-advisor-research/1.0"},
    )
    error = None
    for attempt in range(7):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                raw = response.read()
            write_raw_bytes(archive, raw)
            if delay:
                time.sleep(delay)
            return raw
        except Exception as exc:
            error = exc
            time.sleep(min(3 * (2 ** attempt), 30))
    raise RuntimeError(f"failed TPEx legacy altered {day}") from error


def fetch(
    source_type: str,
    params: dict[str, str],
    archive: Path,
    *,
    cached_only: bool,
    delay: float,
    retries: int = 7,
) -> tuple[dict, str]:
    if archive.exists():
        return read_payload(archive), "cache"
    if cached_only:
        raise FileNotFoundError(archive)
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{ENDPOINTS[source_type]}?{query}",
        headers={
            "User-Agent": "tw-stock-advisor-research/1.0",
            "Referer": "https://www.tpex.org.tw/",
        },
    )
    error = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
            write_payload(archive, payload)
            if delay:
                time.sleep(delay)
            return payload, "download"
        except Exception as exc:  # network retry boundary
            error = exc
            time.sleep(min(3 * (2 ** attempt), 30))
    raise RuntimeError(f"failed TPEx {source_type}: {params}") from error


def table_rows(payload: dict) -> int:
    return sum(len(table.get("data") or []) for table in payload.get("tables") or [])


def source_row(source_type: str, query_key: str, payload: dict, archive: Path) -> dict:
    return {
        "source_type": source_type,
        "query_key": query_key,
        "rows": table_rows(payload),
        "response_date": str(payload.get("date") or ""),
        "raw_path": archive.resolve().relative_to(ROOT.resolve()).as_posix(),
        "raw_sha256": sha256(archive),
    }


def normalize_restriction_dtypes(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for column in (
        "altered_trading", "periodic_trading", "managed_stock", "suspended",
        "financial_focus",
    ):
        frame[column] = frame[column].astype("boolean")
    frame["match_interval_minutes"] = pd.to_numeric(
        frame["match_interval_minutes"], errors="coerce"
    ).astype("Int64")
    return frame


def trading_dates(prices_path: Path, start: date, end: date) -> list[date]:
    frame = pd.read_parquet(prices_path, columns=["trade_date"])
    values = pd.to_datetime(frame["trade_date"], errors="raise").drop_duplicates()
    values = values[values.between(pd.Timestamp(start), pd.Timestamp(end), inclusive="both")]
    return sorted(value.date() for value in values)


def month_ranges(start: date, end: date):
    for period in pd.period_range(start=start, end=end, freq="M"):
        left = max(start, period.start_time.date())
        right = min(end, period.end_time.date())
        yield left, right


def year_ranges(start: date, end: date):
    for year in range(start.year, end.year + 1):
        yield max(start, date(year, 1, 1)), min(end, date(year, 12, 31))


def download_prices(args, dates: list[date]) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    frames: dict[date, pd.DataFrame] = {}
    restriction_frames: dict[date, pd.DataFrame] = {}
    statuses: list[dict] = []

    def one(day: date):
        archive = args.raw_root / "daily_quotes" / str(day.year) / f"dailyQuotes_{day:%Y%m%d}.json.gz"
        payload, _ = fetch(
            "daily_quotes",
            {"date": day.strftime("%Y/%m/%d"), "type": "EW", "response": "json"},
            archive,
            cached_only=args.cached_only,
            delay=args.delay,
        )
        status = [source_row("daily_quotes", day.isoformat(), payload, archive)]
        if day >= date(2008, 4, 9):
            restriction_archive = (
                args.raw_root / "trading_restrictions" / str(day.year) /
                f"chtm_{day:%Y%m%d}.json.gz"
            )
            restriction_payload, _ = fetch(
                "trading_restrictions",
                {"date": day.strftime("%Y/%m/%d"), "response": "json"},
                restriction_archive,
                cached_only=args.cached_only,
                delay=args.delay,
            )
            restrictions = parse_trading_restrictions(restriction_payload, day)
            status.append(source_row(
                "trading_restrictions", day.isoformat(), restriction_payload,
                restriction_archive,
            ))
        else:
            restriction_archive = (
                args.raw_root / "trading_restrictions_legacy" / str(day.year) /
                f"CHTM_{day.year - 1911:02d}{day:%m%d}.html.gz"
            )
            raw = fetch_legacy_html(
                day, restriction_archive,
                cached_only=args.cached_only, delay=args.delay,
            )
            restrictions = parse_legacy_altered_html(raw, day)
            status.append({
                "source_type": "trading_restrictions_legacy",
                "query_key": day.isoformat(),
                "rows": int(len(restrictions)),
                "response_date": day.isoformat(),
                "raw_path": restriction_archive.resolve().relative_to(ROOT.resolve()).as_posix(),
                "raw_sha256": sha256(restriction_archive),
            })
        return day, parse_daily_quotes(payload, day), normalize_restriction_dtypes(restrictions), status

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(one, day): day for day in dates}
        completed = 0
        for future in as_completed(futures):
            day, frame, restrictions, daily_statuses = future.result()
            frames[day] = frame
            restriction_frames[day] = restrictions
            statuses.extend(daily_statuses)
            completed += 1
            if completed % 50 == 0 or completed == len(dates):
                print(f"daily_quotes={completed}/{len(dates)}", flush=True)
    prices = pd.concat([frames[day] for day in dates], ignore_index=True)
    restriction_parts = [restriction_frames[day] for day in dates if not restriction_frames[day].empty]
    restrictions = pd.concat(restriction_parts, ignore_index=True)
    restrictions = normalize_restriction_dtypes(restrictions)
    return (
        prices.sort_values(["trade_date", "stock_id"]).reset_index(drop=True),
        restrictions.sort_values(["snapshot_date", "stock_id"]).reset_index(drop=True),
        statuses,
    )


def download_reference(args, dates: list[date]) -> tuple[dict[str, pd.DataFrame], list[dict], pd.DataFrame]:
    outputs: dict[str, list[pd.DataFrame]] = {
        "corporate_actions": [], "delisted": [], "attention": [], "disposal": [],
    }
    statuses: list[dict] = []
    for left, right in month_ranges(args.start, args.end):
        key = left.strftime("%Y-%m")
        archive = args.raw_root / "corporate_actions" / str(left.year) / f"exDailyQ_{left:%Y%m}.json.gz"
        payload, _ = fetch(
            "corporate_actions",
            {"startDate": left.strftime("%Y/%m/%d"), "endDate": right.strftime("%Y/%m/%d"), "response": "json"},
            archive, cached_only=args.cached_only, delay=args.delay,
        )
        outputs["corporate_actions"].append(parse_ex_daily(payload))
        statuses.append(source_row("corporate_actions", key, payload, archive))

    for left, right in year_ranges(args.start, args.end):
        year = left.year
        archive = args.raw_root / "delisted" / f"deListed_{year}.json.gz"
        payload, _ = fetch(
            "delisted", {"date": str(year), "reason": "-1", "response": "json"},
            archive, cached_only=args.cached_only, delay=args.delay,
        )
        outputs["delisted"].append(parse_delisted(payload))
        statuses.append(source_row("delisted", str(year), payload, archive))

        common = {
            "startDate": left.strftime("%Y/%m/%d"),
            "endDate": right.strftime("%Y/%m/%d"),
            "type": "all", "order": "date", "response": "json",
        }
        for source_type, parser in (("attention", parse_attention), ("disposal", parse_disposal)):
            archive = args.raw_root / source_type / f"{source_type}_{year}.json.gz"
            params = dict(common)
            if source_type == "disposal":
                params.update({"reason": "-1", "measure": "-1"})
            payload, _ = fetch(
                source_type, params, archive,
                cached_only=args.cached_only, delay=args.delay,
            )
            outputs[source_type].append(parser(payload))
            statuses.append(source_row(source_type, str(year), payload, archive))
        print(f"reference_year={year}", flush=True)

    probes = []
    for year in range(args.start.year, args.end.year + 1):
        # Probe a date proven to be a Taiwan trading day, not a fixed calendar
        # day that could be a weekend/holiday. Missing is never converted to zero.
        day = next(value for value in dates if value.year == year)
        archive = args.raw_root / "institutional_probe" / f"dailyTrade_{day:%Y%m%d}.json.gz"
        payload, _ = fetch(
            "institutional_probe",
            {"date": day.strftime("%Y/%m/%d"), "type": "Daily", "response": "json", "sect": "EW"},
            archive, cached_only=args.cached_only, delay=args.delay,
        )
        rows = table_rows(payload)
        probes.append({
            "probe_date": day.isoformat(), "rows": rows,
            "availability": "available" if rows else "missing",
            "missing_semantics": "unknown_not_zero" if rows == 0 else "observed",
            "raw_path": archive.resolve().relative_to(ROOT.resolve()).as_posix(),
            "raw_sha256": sha256(archive),
        })
        statuses.append(source_row("institutional_probe", day.isoformat(), payload, archive))

    result = {}
    for key, frames in outputs.items():
        nonempty = [frame for frame in frames if not frame.empty]
        result[key] = (
            pd.concat(nonempty, ignore_index=True).drop_duplicates().reset_index(drop=True)
            if nonempty else frames[0].iloc[0:0].copy()
        )
    return result, statuses, pd.DataFrame(probes)


def build_universe(prices: pd.DataFrame, delisted: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    universe = prices[["trade_date", "stock_id", "stock_name", "market"]].copy()
    universe = universe.rename(columns={"trade_date": "snapshot_date"})
    universe["asset_type"] = "common_stock"
    universe["is_active"] = True
    universe["membership_source"] = "TPEX_dailyQuotes_standard_board_row"
    universe = universe.sort_values(["snapshot_date", "stock_id"]).reset_index(drop=True)

    grouped = universe.groupby("stock_id", sort=True)
    stocks = grouped.agg(
        stock_name=("stock_name", "last"),
        first_observed_date=("snapshot_date", "min"),
        last_observed_date=("snapshot_date", "max"),
    ).reset_index()
    first_date = universe["snapshot_date"].min()
    stocks["market"] = "TPEX"
    stocks["asset_type"] = "common_stock"
    stocks["listing_date"] = stocks["first_observed_date"]
    stocks["listing_date_is_exact"] = False
    stocks["first_observation_left_censored"] = stocks["first_observed_date"].eq(first_date)
    if not delisted.empty:
        official = delisted.sort_values("delisting_date").drop_duplicates("stock_id", keep="last")
        stocks = stocks.merge(
            official[["stock_id", "delisting_date"]], on="stock_id", how="left", validate="one_to_one"
        )
    else:
        stocks["delisting_date"] = None
    return stocks.sort_values("stock_id").reset_index(drop=True), universe


def write_outputs(args, prices, restrictions, reference, statuses, probes) -> dict:
    actions = reference["corporate_actions"].sort_values(["ex_date", "stock_id"]).reset_index(drop=True)
    delisted = reference["delisted"].sort_values(["delisting_date", "stock_id"]).reset_index(drop=True)
    # The repository's canonical compatibility table is one terminal delisting
    # per code; retain the latest official event if a code appears more than once.
    delisted = delisted.drop_duplicates("stock_id", keep="last").reset_index(drop=True)
    attention = reference["attention"].sort_values(["notice_date", "stock_id", "reason"]).reset_index(drop=True)
    attention = attention.drop_duplicates(["stock_id", "notice_date", "reason"]).reset_index(drop=True)
    disposal = reference["disposal"].sort_values(["start_date", "stock_id", "event_no"]).reset_index(drop=True)
    disposal = disposal.drop_duplicates(["stock_id", "start_date"], keep="last").reset_index(drop=True)
    stocks, universe = build_universe(prices, delisted)
    status = pd.DataFrame(statuses).sort_values(["source_type", "query_key"]).reset_index(drop=True)

    report = {
        "scope": f"TPEX_D5_{args.start}_{args.end}",
        "start": args.start.isoformat(), "end": args.end.isoformat(),
        "trading_dates": int(prices["trade_date"].nunique()),
        "price_rows": int(len(prices)),
        "stock_ids": int(prices["stock_id"].nunique()),
        "suspended_or_missing_price_rows": int(prices["price_missing"].sum()),
        "negative_volume_rows": int(pd.to_numeric(prices["volume"], errors="coerce").lt(0).sum()),
        "negative_turnover_rows": int(pd.to_numeric(prices["turnover"], errors="coerce").lt(0).sum()),
        "nonpositive_issued_shares_rows": int(pd.to_numeric(prices["issued_shares"], errors="coerce").le(0).sum()),
        "corporate_action_rows": int(len(actions)),
        "delisted_rows": int(len(delisted)),
        "attention_rows": int(len(attention)),
        "disposal_rows": int(len(disposal)),
        "trading_restriction_rows": int(len(restrictions)),
        "legacy_restriction_rows_with_partial_flags": int(
            restrictions["source_detail"].str.contains("legacy", na=False).sum()
        ),
        "institutional_probe_rows": int(probes["rows"].sum()),
        "institutional_pre2018_semantics": "missing_unknown_not_zero",
        "units": {
            "price": "TWD_per_share", "volume": "shares", "turnover": "TWD",
            "issued_shares": "shares", "cash_dividend": "TWD_per_share",
            "stock_dividend_value": "TWD_per_share_reference_deduction_not_share_ratio",
        },
        "asset_classification": {
            "rule": "four_digit_numeric_equity_codes_in_TPEX_standard_listed_stock_quotes_table",
            "status": "common_stock_official_table_and_code_convention",
        },
    }
    temporary = args.output_root.with_name(args.output_root.name + f".tmp-{uuid.uuid4().hex}")
    temporary.mkdir(parents=True)
    try:
        prices.to_parquet(temporary / "prices.parquet", index=False)
        actions.to_parquet(temporary / "dividend_events.parquet", index=False)
        stocks.to_parquet(temporary / "stocks.parquet", index=False)
        delisted.to_parquet(temporary / "delisted_stocks.parquet", index=False)
        universe.to_parquet(temporary / "stock_universe_history.parquet", index=False)
        attention.to_parquet(temporary / "notice_events.parquet", index=False)
        disposal.to_parquet(temporary / "disposition_events.parquet", index=False)
        restrictions.to_parquet(temporary / "trading_restrictions.parquet", index=False)
        status.to_parquet(temporary / "tpex_source_status.parquet", index=False)
        probes.sort_values("probe_date").to_parquet(
            temporary / "institutional_availability.parquet", index=False
        )
        (temporary / "quality_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if args.output_root.exists():
            shutil.rmtree(args.output_root)
        os.replace(temporary, args.output_root)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, default=date(2008, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2014, 12, 31))
    parser.add_argument(
        "--calendar-prices", type=Path,
        default=ROOT / "data" / "research_versions" / "twse_prices_2005_2014_v1" / "prices.parquet",
    )
    parser.add_argument("--raw-root", type=Path, default=ROOT / "data" / "raw" / "tpex" / "d5")
    parser.add_argument(
        "--output-root", type=Path,
        default=ROOT / "data" / "research" / "tpex_history_2008_2014",
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--delay", type=float, default=0.1)
    parser.add_argument("--cached-only", action="store_true")
    args = parser.parse_args()
    dates = trading_dates(args.calendar_prices, args.start, args.end)
    if not dates:
        raise ValueError("no official Taiwan trading dates in requested range")
    prices, restrictions, price_status = download_prices(args, dates)
    reference, other_status, probes = download_reference(args, dates)
    report = write_outputs(
        args, prices, restrictions, reference, price_status + other_status, probes
    )
    print(args.output_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
