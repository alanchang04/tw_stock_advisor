"""Archive monthly TWSE issued shares and point-in-time industry membership."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.parse
import urllib.request
import uuid

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.twse_market_structure import (  # noqa: E402
    INDUSTRY_CATEGORIES,
    build_monthly_market_structure,
    parse_mi_qfiis,
    resolve_industry_membership,
)


API_URL = "https://wwwc.twse.com.tw/rwd/zh/fund/MI_QFIIS"
ALL_SELECT_TYPE = "ALLBUT0999"


def raw_path(raw_root: Path, snapshot_date: date, select_type: str) -> Path:
    return (
        raw_root / f"{snapshot_date.year:04d}" /
        f"MI_QFIIS_{snapshot_date:%Y%m%d}_{select_type}.json.gz"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _write_payload(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        temporary.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_payload(path: Path) -> dict:
    return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


def fetch_payload(
    snapshot_date: date,
    select_type: str,
    raw_root: Path,
    cached_only: bool,
    delay: float,
    retries: int = 8,
) -> tuple[dict, Path, str]:
    archive = raw_path(raw_root, snapshot_date, select_type)
    if archive.exists():
        return _read_payload(archive), archive, "cache"
    if cached_only:
        raise FileNotFoundError(archive)
    query = urllib.parse.urlencode({
        "date": snapshot_date.strftime("%Y%m%d"),
        "selectType": select_type,
        "response": "json",
    })
    request = urllib.request.Request(
        f"{API_URL}?{query}",
        headers={
            "User-Agent": "tw-stock-advisor-research/1.0",
            "Referer": "https://www.twse.com.tw/zh/trading/foreign/mi-qfiis.html",
        },
    )
    error = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if str(payload.get("stat")) != "OK":
                raise ValueError(
                    f"TWSE status {payload.get('stat')!r} for {snapshot_date}/{select_type}"
                )
            _write_payload(archive, payload)
            if delay:
                time.sleep(delay)
            return payload, archive, "download"
        except Exception as exc:  # network retry boundary
            error = exc
            time.sleep(min(5 * (2 ** attempt), 30))
    raise RuntimeError(f"failed {snapshot_date}/{select_type}") from error


def month_end_trading_dates(prices: pd.DataFrame, start: date, end: date) -> list[date]:
    dates = pd.to_datetime(prices["trade_date"], errors="raise")
    dates = dates[dates.between(pd.Timestamp(start), pd.Timestamp(end), inclusive="both")]
    frame = pd.DataFrame({"trade_date": dates.drop_duplicates()})
    frame["month"] = frame["trade_date"].dt.to_period("M")
    return [value.date() for value in frame.groupby("month")["trade_date"].max()]


def build_outputs(args) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    prices = pd.read_parquet(args.prices, columns=["stock_id", "trade_date", "close"])
    prices["trade_date"] = pd.to_datetime(prices["trade_date"]).dt.date.astype(str)
    master = pd.read_parquet(args.security_master / "security_master_staging.parquet")
    universe = pd.read_parquet(args.security_master / "stock_universe_history.parquet")
    dates = month_end_trading_dates(prices, args.start, args.end)
    expected_months = (args.end.year - args.start.year) * 12 + args.end.month - args.start.month + 1
    if len(dates) != expected_months:
        raise ValueError(f"expected {expected_months} monthly dates, found {len(dates)}")

    all_frames = []
    status_rows = []
    industry_dates = set(
        dates if args.industry_frequency == "monthly"
        else [value for value in dates if value.month == 1]
    )
    latest_categories = None
    latest_industry_date = None
    for date_number, snapshot_date in enumerate(dates, start=1):
        select_types = [ALL_SELECT_TYPE]
        if snapshot_date in industry_dates:
            select_types.extend(INDUSTRY_CATEGORIES)
        payloads = {}
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(
                    fetch_payload, snapshot_date, select_type, args.raw_root,
                    args.cached_only, args.delay,
                ): select_type
                for select_type in select_types
            }
            for future in as_completed(futures):
                select_type = futures[future]
                payload, archive, source = future.result()
                payloads[select_type] = payload
                status_rows.append({
                    "snapshot_date": snapshot_date.isoformat(),
                    "select_type": select_type,
                    "source_status": source,
                    "rows": len(payload.get("data") or []),
                    "stat": str(payload.get("stat")),
                    "raw_path": archive.resolve().relative_to(ROOT.resolve()).as_posix(),
                    "raw_sha256": _sha256(archive),
                })

        issued = parse_mi_qfiis(payloads[ALL_SELECT_TYPE], snapshot_date)
        issued_index = issued.set_index("stock_id")["issued_shares"]
        if snapshot_date in industry_dates:
            category_ids = {}
            for code in INDUSTRY_CATEGORIES:
                category_frame = parse_mi_qfiis(payloads[code], snapshot_date)
                category_ids[code] = set(category_frame["stock_id"].astype(str))
                unknown = category_ids[code] - set(issued_index.index)
                if unknown:
                    raise ValueError(
                        f"{snapshot_date}/{code} contains stocks absent from ALL: "
                        f"{sorted(unknown)[:5]}"
                    )
                if not category_frame.empty:
                    comparison = category_frame.set_index("stock_id")["issued_shares"]
                    mismatch = comparison.ne(issued_index.loc[comparison.index])
                    if mismatch.any():
                        raise ValueError(f"issued shares mismatch in {snapshot_date}/{code}")
            latest_categories = resolve_industry_membership(category_ids)
            latest_industry_date = snapshot_date
        if latest_categories is None or latest_industry_date is None:
            raise ValueError("no prior point-in-time industry observation is available")
        monthly = build_monthly_market_structure(
            issued, latest_categories,
            prices[prices["trade_date"].eq(snapshot_date.isoformat())], master,
        )
        monthly["industry_observation_date"] = latest_industry_date.isoformat()
        monthly["industry_stale_days"] = (snapshot_date - latest_industry_date).days
        monthly["industry_observation_is_exact_snapshot"] = (
            snapshot_date == latest_industry_date
        )
        all_frames.append(monthly)
        print(
            f"months={date_number}/{len(dates)} date={snapshot_date} "
            f"official_rows={len(issued)} common_rows={len(monthly)}",
            flush=True,
        )

    result = pd.concat(all_frames, ignore_index=True)
    status = pd.DataFrame(status_rows).sort_values(
        ["snapshot_date", "select_type"]
    ).reset_index(drop=True)
    universe["snapshot_date"] = pd.to_datetime(universe["snapshot_date"])
    result_month = pd.to_datetime(result["snapshot_date"]).dt.to_period("M")
    universe_month = universe["snapshot_date"].dt.to_period("M")
    coverage = []
    for month in sorted(set(result_month)):
        expected_ids = set(universe.loc[
            universe_month.eq(month) & universe["asset_type"].eq("common_stock")
            & universe["is_active"].eq(True), "stock_id"
        ].astype(str))
        observed = result[result_month.eq(month)]
        observed_ids = set(observed["stock_id"].astype(str))
        coverage.append({
            "month": str(month), "expected_common": len(expected_ids),
            "observed_common": len(observed_ids),
            "issued_share_coverage": len(expected_ids & observed_ids) / len(expected_ids),
            "industry_coverage": float(observed["industry_code_asof"].notna().mean()),
            "same_day_market_cap_coverage": float(observed["market_cap_twd"].notna().mean()),
            "missing_expected_ids": sorted(expected_ids - observed_ids),
        })
    report = {
        "start": args.start.isoformat(), "end": args.end.isoformat(),
        "monthly_snapshots": len(dates), "rows": len(result),
        "stock_ids": int(result["stock_id"].nunique()),
        "source_queries": len(status),
        "issued_shares_unit": "shares",
        "nonpositive_issued_shares": int(result["issued_shares"].le(0).sum()),
        "nonintegral_issued_shares": int(
            result["issued_shares"].ne(result["issued_shares"].astype("int64")).sum()
        ),
        "industry_pit_coverage": float(result["industry_code_asof"].notna().mean()),
        "industry_frequency": args.industry_frequency,
        "exact_industry_snapshot_rows": int(
            result["industry_observation_is_exact_snapshot"].sum()
        ),
        "maximum_industry_stale_days": int(result["industry_stale_days"].max()),
        "same_day_market_cap_coverage": float(result["market_cap_twd"].notna().mean()),
        "minimum_monthly_issued_share_coverage": min(
            row["issued_share_coverage"] for row in coverage
        ),
        "monthly_coverage": coverage,
    }
    return result, status, report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, default=date(2005, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2014, 12, 31))
    parser.add_argument(
        "--prices", type=Path,
        default=ROOT / "data" / "research_versions" / "twse_prices_2005_2014_v1" /
        "prices.parquet",
    )
    parser.add_argument(
        "--security-master", type=Path,
        default=ROOT / "data" / "research_versions" /
        "twse_security_master_2005_2014_staging_v1",
    )
    parser.add_argument(
        "--raw-root", type=Path,
        default=ROOT / "data" / "raw" / "twse" / "market_structure" / "mi_qfiis",
    )
    parser.add_argument(
        "--output-root", type=Path,
        default=ROOT / "data" / "research" / "twse_market_structure_2005_2014",
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument(
        "--industry-frequency", choices=["annual", "monthly"], default="annual",
    )
    parser.add_argument("--cached-only", action="store_true")
    args = parser.parse_args()

    result, status, report = build_outputs(args)
    temporary = args.output_root.with_name(
        args.output_root.name + f".tmp-{uuid.uuid4().hex}"
    )
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    try:
        result.to_parquet(temporary / "market_structure_monthly.parquet", index=False)
        # Cache/download status is a build circumstance, not source identity.
        stable_status = status.drop(columns=["source_status"])
        stable_status.to_parquet(
            temporary / "market_structure_source_status.parquet", index=False
        )
        pd.DataFrame([
            {"industry_code": code, "industry_name": name}
            for code, name in INDUSTRY_CATEGORIES.items()
        ]).sort_values("industry_code").to_parquet(
            temporary / "industry_categories.parquet", index=False
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
    print(json.dumps({key: value for key, value in report.items() if key != "monthly_coverage"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
