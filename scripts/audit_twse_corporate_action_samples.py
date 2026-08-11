"""Deterministically compare staged corporate actions with archived TWSE rows."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.backfill_twse_corporate_actions import (
    REPORT_EX_RIGHT,
    REPORT_REDUCTION,
    parse_twt49u,
    parse_twtauu,
    read_raw_response,
    sha256_file,
)

COMPARE_COLUMNS = [
    "source_report", "stock_id", "stock_name", "event_date", "event_kind",
    "pre_event_close", "reference_price", "opening_reference_price",
    "ex_right_reference_price", "rights_value", "cash_value", "combined_value",
    "upper_limit", "lower_limit", "reduction_reason", "detail_key",
    "adjustment_factor",
]


def _resolve_raw_path(value: str, repo_root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


def _portable_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _same(left, right) -> bool:
    if pd.isna(left) and pd.isna(right):
        return True
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        try:
            return abs(float(left) - float(right)) <= 1e-10
        except (TypeError, ValueError):
            pass
    return str(left) == str(right)


def select_samples(source_rows: pd.DataFrame, per_group: int = 3) -> pd.DataFrame:
    """Take first/middle/last rows for every report-year group."""
    frame = source_rows.copy()
    frame["event_year"] = pd.to_datetime(frame["event_date"]).dt.year
    selected = []
    for _, group in frame.groupby(["source_report", "event_year"], sort=True):
        group = group.sort_values(
            ["event_date", "stock_id", "period_start", "source_row_number"],
            kind="stable",
        )
        count = min(per_group, len(group))
        positions = sorted(set(round(index * (len(group) - 1) / max(count - 1, 1))
                               for index in range(count)))
        selected.append(group.iloc[positions])
    return pd.concat(selected, ignore_index=True).drop(columns="event_year")


def audit_samples(snapshot_dir: Path, repo_root: Path = ROOT,
                  minimum_samples: int = 30) -> tuple[dict, pd.DataFrame]:
    source_rows = pd.read_parquet(snapshot_dir / "corporate_action_source_rows.parquet")
    samples = select_samples(source_rows)
    results = []
    payload_cache: dict[Path, dict] = {}
    for staged in samples.to_dict("records"):
        raw_path = _resolve_raw_path(staged["raw_path"], repo_root).resolve()
        payload = payload_cache.setdefault(raw_path, read_raw_response(raw_path))
        raw_index = int(staged["source_row_number"]) - 1
        raw_values = payload["data"][raw_index]
        raw_mapping = {
            str(field): raw_values[index] if index < len(raw_values) else None
            for index, field in enumerate(payload.get("fields") or [])
        }
        parser = parse_twt49u if staged["source_report"] == REPORT_EX_RIGHT else parse_twtauu
        parsed = parser({"fields": payload.get("fields") or [], "data": [raw_values]}).iloc[0]
        mismatches = [column for column in COMPARE_COLUMNS
                      if not _same(staged.get(column), parsed.get(column))]
        raw_row_json = json.dumps(raw_mapping, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"))
        results.append({
            "source_report": staged["source_report"],
            "event_date": staged["event_date"],
            "stock_id": staged["stock_id"],
            "stock_name": staged["stock_name"],
            "period_start": staged["period_start"],
            "source_row_number": int(staged["source_row_number"]),
            "raw_path": _portable_path(raw_path, repo_root),
            "raw_sha256_matches": sha256_file(raw_path) == staged["raw_sha256"],
            "normalized_fields_match": not mismatches,
            "mismatched_fields": ",".join(mismatches),
            "raw_row_sha256": hashlib.sha256(raw_row_json.encode("utf-8")).hexdigest().upper(),
            "raw_official_row_json": raw_row_json,
        })
    result_frame = pd.DataFrame(results)
    report = {
        "source_rows_sha256": sha256_file(
            snapshot_dir / "corporate_action_source_rows.parquet"
        ),
        "selection_rule": "first_middle_last_per_source_report_and_event_year",
        "sample_rows": len(result_frame),
        "minimum_required": minimum_samples,
        "source_report_counts": result_frame["source_report"].value_counts().sort_index().to_dict(),
        "year_counts": pd.to_datetime(result_frame["event_date"]).dt.year.value_counts().sort_index().to_dict(),
        "raw_sha256_mismatches": int((~result_frame["raw_sha256_matches"]).sum()),
        "normalized_field_mismatches": int((~result_frame["normalized_fields_match"]).sum()),
        "passed": bool(
            len(result_frame) >= minimum_samples
            and result_frame["raw_sha256_matches"].all()
            and result_frame["normalized_fields_match"].all()
        ),
    }
    return report, result_frame


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--report-json", type=Path, required=True)
    parser.add_argument("--samples-csv", type=Path, required=True)
    parser.add_argument("--minimum-samples", type=int, default=30)
    args = parser.parse_args()

    report, samples = audit_samples(args.snapshot_dir, minimum_samples=args.minimum_samples)
    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.samples_csv.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
    samples.to_csv(args.samples_csv, index=False, encoding="utf-8-sig")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
