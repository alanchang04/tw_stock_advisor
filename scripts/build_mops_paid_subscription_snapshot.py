"""Build an immutable normalized snapshot from cached MOPS subscription facts."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import uuid
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.snapshot_manifest import build_manifest, build_quality_report, write_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--facts", type=Path, nargs="+",
        default=[
            ROOT / "data" / "research" / "mops_paid_subscription_facts_2005_2014.parquet",
            ROOT / "data" / "research" /
            "mops_paid_subscription_facts_previous_year_early_events.parquet",
        ],
    )
    parser.add_argument(
        "--snapshot-id", default="mops_paid_subscription_announcements_2004_2014_v1",
    )
    parser.add_argument(
        "--output-root", type=Path, default=ROOT / "data" / "research_versions",
    )
    args = parser.parse_args()

    target = args.output_root / args.snapshot_id
    if target.exists():
        raise FileExistsError(f"snapshot already exists and will not be overwritten: {target}")
    frames = [pd.read_parquet(path) for path in args.facts]
    facts = pd.concat(frames, ignore_index=True).drop_duplicates([
        "stock_id", "enter_date", "serial_number", "fact_type", "fact_value", "fact_date",
    ])
    facts = facts.sort_values([
        "announcement_date", "stock_id", "enter_date", "serial_number",
        "fact_type", "fact_value", "fact_date",
    ]).reset_index(drop=True)
    statuses = []
    for path in args.facts:
        status_path = path.with_name(f"{path.stem}_source_status.parquet")
        statuses.append(pd.read_parquet(status_path))
    status = pd.concat(statuses, ignore_index=True).drop_duplicates([
        "stock_id", "roc_year", "history_raw_sha256",
    ]).sort_values(["roc_year", "stock_id"]).reset_index(drop=True)
    # Download-vs-cache is a build circumstance, not source identity.
    status = status.drop(columns=["history_source", "details_downloaded", "details_cached"])

    args.output_root.mkdir(parents=True, exist_ok=True)
    temporary = args.output_root / f".{args.snapshot_id}.tmp-{uuid.uuid4().hex}"
    temporary.mkdir()
    try:
        facts.to_parquet(temporary / "subscription_facts.parquet", index=False)
        status.to_parquet(temporary / "source_status.parquet", index=False)
        manifest = build_manifest(temporary, ROOT)
        manifest.update({
            "snapshot_id": args.snapshot_id,
            "snapshot_dir": target.resolve().relative_to(ROOT.resolve()).as_posix(),
            "source": {
                "provider": "MOPS",
                "api_endpoints": ["t05st01", "t05st01_detail"],
                "purpose": "paid_subscription_terms_and_settlement_announcements",
                "source_queries": len(status),
            },
        })
        quality = build_quality_report(
            temporary, manifest,
            required_files={"subscription_facts.parquet", "source_status.parquet"},
        )
        counts = facts["fact_type"].value_counts().to_dict()
        quality.update({
            "snapshot_id": args.snapshot_id,
            "scope": "D3_MOPS_paid_subscription_announcements",
            "fact_rows": len(facts),
            "stock_ids": int(facts["stock_id"].nunique()),
            "fact_counts": {str(key): int(value) for key, value in counts.items()},
            "structural_passed": bool(
                quality.get("structural_passed")
                and facts.loc[facts["fact_value"].notna(), "fact_value"].gt(0).all()
            ),
            "promotion_ready": False,
            "blockers": [
                "announcement facts require a unique TWSE reference-equation match",
                "subscription payment and new-share delivery dates are not yet complete",
            ],
        })
        write_json(temporary / "manifest.json", manifest)
        write_json(temporary / "quality_report.json", quality)
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(target)
    print(manifest["content_sha256"])


if __name__ == "__main__":
    main()
