"""Build an immutable TWSE disposition component from verified annual raw files."""
from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data_pipeline.fetchers.disposition_fetcher import parse_punish_rows  # noqa: E402
from research.snapshot_manifest import (  # noqa: E402
    build_manifest,
    build_quality_report,
    sha256_file,
    write_json,
)
from scripts.backfill_twse_disposition_history import (  # noqa: E402
    raw_path,
    read_raw_response,
)
from scripts.build_data_transfer_manifest import verify_manifest  # noqa: E402


REQUIRED = {
    "disposition_events.parquet",
    "twse_disposition_source_status.parquet",
}


def build_snapshot_data(
    raw_root: Path,
    start_year: int,
    end_year: int,
    data_base: Path = ROOT,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Normalize every required annual punish archive with per-file lineage."""
    event_frames: list[pd.DataFrame] = []
    status_rows: list[dict] = []
    for year in range(start_year, end_year + 1):
        archive = raw_path(raw_root, "punish", year)
        if not archive.is_file():
            raise FileNotFoundError(f"required TWSE punish archive is missing: {archive}")
        payload = read_raw_response(archive)
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            raise ValueError(f"punish {year}: data is not a list")
        frame = parse_punish_rows(rows)
        raw_sha = sha256_file(archive)
        portable = archive.resolve().relative_to(data_base.resolve()).as_posix()
        if not frame.empty:
            frame = frame.copy()
            frame["source_year"] = year
            frame["raw_path"] = portable
            frame["raw_sha256"] = raw_sha
            event_frames.append(frame)
        status_rows.append({
            "report": "punish",
            "year": year,
            "official_stat": str(payload.get("stat") or ""),
            "raw_rows": len(rows),
            "normalized_common_stock_rows": len(frame),
            "filtered_or_unparseable_rows": len(rows) - len(frame),
            "raw_path": portable,
            "raw_sha256": raw_sha,
        })

    if event_frames:
        events = pd.concat(event_frames, ignore_index=True)
        events = events.sort_values(
            ["stock_id", "start_date", "announce_date", "source_year"],
            na_position="first",
        )
        events = events.drop_duplicates(["stock_id", "start_date"], keep="last")
        events = events.sort_values(["start_date", "stock_id"]).reset_index(drop=True)
    else:
        events = pd.DataFrame(columns=[
            "stock_id", "announce_date", "start_date", "end_date", "cumulative",
            "reason", "measure", "market", "source_year", "raw_path", "raw_sha256",
        ])
    status = pd.DataFrame(status_rows).sort_values(["report", "year"]).reset_index(drop=True)
    return events, status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2005)
    parser.add_argument("--end-year", type=int, default=2014)
    parser.add_argument(
        "--raw-root", type=Path,
        default=ROOT / "data" / "raw" / "twse" / "disposition",
    )
    parser.add_argument(
        "--raw-manifest", type=Path,
        default=ROOT / "reports" / "twse_disposition_transfer_manifest_2005_2014.json",
    )
    parser.add_argument(
        "--output-root", type=Path, default=ROOT / "data" / "research_versions",
    )
    parser.add_argument(
        "--snapshot-id", default="twse_disposition_punish_2005_2014_v1",
    )
    parser.add_argument(
        "--data-base", type=Path, default=ROOT,
        help="repository root containing raw files and the transfer manifest",
    )
    args = parser.parse_args()
    if args.start_year > args.end_year:
        raise ValueError("start-year must not exceed end-year")

    raw_manifest = json.loads(args.raw_manifest.read_text(encoding="utf-8"))
    raw_verification = verify_manifest(args.data_base, raw_manifest)
    if not raw_verification["passed"]:
        raise ValueError(f"raw transfer manifest verification failed: {raw_verification}")

    events, status = build_snapshot_data(
        args.raw_root, args.start_year, args.end_year, args.data_base,
    )
    target = args.output_root / args.snapshot_id
    if target.exists():
        raise FileExistsError(f"snapshot already exists and will not be overwritten: {target}")
    temporary = args.output_root / f".{args.snapshot_id}.tmp-{uuid.uuid4().hex}"
    temporary.mkdir(parents=True)
    try:
        events.to_parquet(temporary / "disposition_events.parquet", index=False)
        status.to_parquet(
            temporary / "twse_disposition_source_status.parquet", index=False,
        )
        manifest = build_manifest(temporary, ROOT)
        if not manifest["git"].get("commit"):
            raise RuntimeError(
                "release snapshot requires a readable Git commit; build from a trusted worktree"
            )
        if manifest["git"].get("dirty"):
            raise RuntimeError(
                "release snapshot requires a clean Git worktree"
            )
        manifest.update({
            "snapshot_id": args.snapshot_id,
            "snapshot_dir": target.resolve().relative_to(
                args.data_base.resolve()
            ).as_posix(),
            "source": {
                "provider": "TWSE",
                "report": "punish",
                "official_endpoint": (
                    "https://www.twse.com.tw/rwd/zh/announcement/punish"
                ),
                "start_year": args.start_year,
                "end_year": args.end_year,
                "raw_manifest": args.raw_manifest.resolve().relative_to(
                    args.data_base.resolve()
                ).as_posix(),
                "raw_manifest_sha256": sha256_file(args.raw_manifest),
                "raw_collection_sha256": raw_manifest["collection_sha256"],
            },
        })
        base_quality = build_quality_report(
            temporary, manifest, required_files=REQUIRED,
        )
        invalid_intervals = int(
            (pd.to_datetime(events["end_date"]) < pd.to_datetime(events["start_date"])).sum()
        ) if not events.empty else 0
        expected_years = args.end_year - args.start_year + 1
        source_complete = bool(
            len(status) == expected_years
            and status["year"].nunique() == expected_years
            and status["official_stat"].eq("OK").all()
            and status["raw_sha256"].str.fullmatch(r"[0-9A-F]{64}").all()
        )
        normalized_source_rows = int(status["normalized_common_stock_rows"].sum())
        cross_year_duplicate_rows = normalized_source_rows - len(events)
        component_ready = bool(
            base_quality["structural_passed"]
            and raw_verification["passed"]
            and source_complete
            and invalid_intervals == 0
            and not events.empty
        )
        quality = {
            **base_quality,
            "scope": f"TWSE_punish_{args.start_year}_{args.end_year}",
            "rows": int(len(events)),
            "raw_rows": int(status["raw_rows"].sum()),
            "normalized_source_rows_before_cross_year_deduplication": (
                normalized_source_rows
            ),
            "cross_year_duplicate_rows": cross_year_duplicate_rows,
            "stock_ids": int(events["stock_id"].nunique()) if not events.empty else 0,
            "start_date_min": str(events["start_date"].min()) if not events.empty else None,
            "end_date_max": str(events["end_date"].max()) if not events.empty else None,
            "source_years_expected": expected_years,
            "source_years_observed": int(status["year"].nunique()),
            "source_complete": source_complete,
            "raw_manifest_verified": raw_verification["passed"],
            "raw_collection_sha256": raw_manifest["collection_sha256"],
            "invalid_intervals": invalid_intervals,
            "twse_disposition_component_ready": component_ready,
            "promotion_ready": component_ready,
            "notice_status": "not_in_MOM1_required_scope_not_downloaded",
            "known_gaps": [
                "TWSE notice events are not part of this MOM-1 disposition component",
                "TPEx disposition history is supplied by the separate D5 snapshot",
            ],
        }
        write_json(temporary / "manifest.json", manifest)
        write_json(temporary / "quality_report.json", quality)
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(target)
    print(manifest["content_sha256"])
    print(json.dumps({
        "rows": quality["rows"],
        "stock_ids": quality["stock_ids"],
        "component_ready": component_ready,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
