"""Archive MOPS material events relevant to paid subscription rights."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.mops_paid_subscription import (
    SETTLEMENT_SUBJECT_PATTERN,
    SUBJECT_PATTERN,
    detail_text,
    extract_subscription_facts,
    extract_subscription_settlement_facts,
    history_records,
)


API_ROOT = "https://mops.twse.com.tw/mops/api"


def _post(endpoint: str, body: dict) -> dict:
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"{API_ROOT}/{endpoint}", data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "tw-stock-advisor-research/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def _write_gzip_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))


def _read_gzip_json(path: Path) -> dict:
    return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def history_path(root: Path, stock_id: str, roc_year: int) -> Path:
    return root / "history" / str(roc_year + 1911) / f"{stock_id}_{roc_year}.json.gz"


def detail_path(root: Path, stock_id: str, enter_date: str, serial: str) -> Path:
    return root / "detail" / str(int(enter_date[:3]) + 1911) / f"{stock_id}_{enter_date}_{serial}.json.gz"


def fetch_or_cache(path: Path, endpoint: str, body: dict, cached_only: bool) -> tuple[dict, str]:
    if path.exists():
        return _read_gzip_json(path), "cache"
    if cached_only:
        raise FileNotFoundError(path)
    payload = _post(endpoint, body)
    _write_gzip_json(path, payload)
    return payload, "download"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ledger", type=Path,
        default=ROOT / "reports" / "twse_execution_action_ledger_2005_2014.csv",
    )
    parser.add_argument(
        "--raw-root", type=Path,
        default=ROOT / "data" / "raw" / "mops" / "paid_subscription_announcements",
    )
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "data" / "research" / "mops_paid_subscription_facts_2005_2014.parquet",
    )
    parser.add_argument("--start-roc-year", type=int, default=94)
    parser.add_argument("--end-roc-year", type=int, default=103)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--previous-year-for-event-month-through", type=int,
        help="query the previous ROC year for events in months 1..N",
    )
    parser.add_argument(
        "--following-year-for-event-month-from", type=int,
        help="query the following ROC year for events in months N..12",
    )
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--cached-only", action="store_true")
    args = parser.parse_args()

    ledger = pd.read_csv(args.ledger, dtype={"stock_id": str})
    paid = ledger[ledger["ledger_block_reason"].isin([
        "paid_subscription_terms_missing",
        "paid_subscription_settlement_timing_missing",
    ])].copy()
    paid["roc_year"] = pd.to_datetime(paid["event_date"]).dt.year - 1911
    if args.previous_year_for_event_month_through is not None:
        event_month = pd.to_datetime(paid["event_date"]).dt.month
        paid = paid[event_month.le(args.previous_year_for_event_month_through)].copy()
        paid["roc_year"] = paid["roc_year"] - 1
    if args.following_year_for_event_month_from is not None:
        event_month = pd.to_datetime(paid["event_date"]).dt.month
        paid = paid[event_month.ge(args.following_year_for_event_month_from)].copy()
        paid["roc_year"] = paid["roc_year"] + 1
    jobs = paid[
        paid["roc_year"].between(args.start_roc_year, args.end_roc_year)
    ][["stock_id", "roc_year"]].drop_duplicates().sort_values(["roc_year", "stock_id"])
    if args.limit is not None:
        jobs = jobs.head(max(args.limit, 0))

    fact_rows = []
    source_rows = []
    for job_number, job in enumerate(jobs.itertuples(index=False), start=1):
        stock_id, roc_year = str(job.stock_id), int(job.roc_year)
        path = history_path(args.raw_root, stock_id, roc_year)
        history, history_source = fetch_or_cache(path, "t05st01", {
            "companyId": stock_id, "year": str(roc_year), "month": "all",
            "firstDay": "", "lastDay": "",
        }, args.cached_only)
        candidates = [
            record for record in history_records(history)
            if SUBJECT_PATTERN.search(record["subject"])
            or SETTLEMENT_SUBJECT_PATTERN.search(record["subject"])
        ]
        details_downloaded = details_cached = 0
        for record in candidates:
            detail_archive = detail_path(
                args.raw_root, stock_id, record["enter_date"], record["serial_number"]
            )
            detail, detail_source = fetch_or_cache(detail_archive, "t05st01_detail", {
                "companyId": stock_id,
                "marketKind": record["market_kind"],
                "enterDate": record["enter_date"],
                "serialNumber": record["serial_number"],
            }, args.cached_only)
            details_downloaded += detail_source == "download"
            details_cached += detail_source == "cache"
            body = detail_text(detail)
            facts = extract_subscription_facts(body)
            settlement_facts = extract_subscription_settlement_facts(body)
            for fact_type, values in (
                ("subscription_rate", facts["subscription_rates"]),
                ("subscription_price", facts["subscription_prices"]),
                ("shareholder_allocation_fraction", facts["shareholder_allocation_fractions"]),
            ):
                for value in values:
                    fact_rows.append({
                        **record,
                        "query_roc_year": roc_year,
                        "fact_type": fact_type,
                        "fact_value": value,
                        "fact_date": None,
                        "body": body,
                        "history_raw_path": path.resolve().relative_to(ROOT.resolve()).as_posix(),
                        "history_raw_sha256": _sha256(path),
                        "detail_raw_path": detail_archive.resolve().relative_to(ROOT.resolve()).as_posix(),
                        "detail_raw_sha256": _sha256(detail_archive),
                    })
            for fact_type, values in settlement_facts.items():
                for value in values:
                    fact_rows.append({
                        **record,
                        "query_roc_year": roc_year,
                        "fact_type": fact_type,
                        "fact_value": None,
                        "fact_date": value,
                        "body": body,
                        "history_raw_path": path.resolve().relative_to(ROOT.resolve()).as_posix(),
                        "history_raw_sha256": _sha256(path),
                        "detail_raw_path": detail_archive.resolve().relative_to(ROOT.resolve()).as_posix(),
                        "detail_raw_sha256": _sha256(detail_archive),
                    })
            if detail_source == "download" and args.delay:
                time.sleep(args.delay)
        source_rows.append({
            "stock_id": stock_id,
            "roc_year": roc_year,
            "history_source": history_source,
            "candidate_announcements": len(candidates),
            "details_downloaded": details_downloaded,
            "details_cached": details_cached,
            "history_raw_path": path.resolve().relative_to(ROOT.resolve()).as_posix(),
            "history_raw_sha256": _sha256(path),
        })
        if history_source == "download" and args.delay:
            time.sleep(args.delay)
        if job_number % 10 == 0 or job_number == len(jobs):
            print(
                f"jobs={job_number}/{len(jobs)} facts={len(fact_rows)} "
                f"last={stock_id}/{roc_year}", flush=True,
            )

    facts = pd.DataFrame(fact_rows)
    status = pd.DataFrame(source_rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    facts.to_parquet(args.output, index=False)
    status.to_parquet(args.output.with_name(f"{args.output.stem}_source_status.parquet"), index=False)
    print(json.dumps({
        "jobs": len(jobs), "fact_rows": len(facts),
        "rate_facts": int(facts["fact_type"].eq("subscription_rate").sum()) if len(facts) else 0,
        "price_facts": int(facts["fact_type"].eq("subscription_price").sum()) if len(facts) else 0,
        "allocation_facts": int(
            facts["fact_type"].eq("shareholder_allocation_fraction").sum()
        ) if len(facts) else 0,
        "settlement_date_facts": int(facts["fact_date"].notna().sum()) if len(facts) else 0,
        "output": str(args.output),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
