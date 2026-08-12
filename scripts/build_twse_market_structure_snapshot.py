"""Build an immutable D4 market-structure snapshot from normalized staging data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.snapshot_manifest import build_manifest, build_quality_report, write_json


REQUIRED = {
    "market_structure_monthly.parquet",
    "market_structure_source_status.parquet",
    "industry_categories.parquet",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source", type=Path,
        default=ROOT / "data" / "research" / "twse_market_structure_2005_2014",
    )
    parser.add_argument(
        "--snapshot-id", default="twse_market_structure_2005_2014_staging_v1",
    )
    parser.add_argument(
        "--output-root", type=Path, default=ROOT / "data" / "research_versions",
    )
    args = parser.parse_args()
    target = args.output_root / args.snapshot_id
    if target.exists():
        raise FileExistsError(f"snapshot already exists and will not be overwritten: {target}")
    temporary = args.output_root / f".{args.snapshot_id}.tmp-{uuid.uuid4().hex}"
    temporary.mkdir(parents=True)
    try:
        for name in REQUIRED:
            shutil.copy2(args.source / name, temporary / name)
        data = pd.read_parquet(temporary / "market_structure_monthly.parquet")
        status = pd.read_parquet(temporary / "market_structure_source_status.parquet")
        source_quality = json.loads(
            (args.source / "quality_report.json").read_text(encoding="utf-8")
        )
        manifest = build_manifest(temporary, ROOT)
        manifest.update({
            "snapshot_id": args.snapshot_id,
            "snapshot_dir": target.resolve().relative_to(ROOT.resolve()).as_posix(),
            "source": {
                "provider": "TWSE",
                "endpoint": "https://wwwc.twse.com.tw/rwd/zh/fund/MI_QFIIS",
                "official_page": "https://www.twse.com.tw/zh/trading/foreign/mi-qfiis.html",
                "source_queries": int(len(status)),
                "price_snapshot": "twse_prices_2005_2014_v1",
                "security_master_snapshot": "twse_security_master_2005_2014_staging_v1",
            },
        })
        quality = build_quality_report(temporary, manifest, required_files=REQUIRED)
        lookahead_rows = pd.to_datetime(data["industry_observation_date"]).gt(
            pd.to_datetime(data["snapshot_date"])
        )
        no_lookahead = not lookahead_rows.any()
        equation_error = (
            data.loc[data["market_cap_twd"].notna(), "market_cap_twd"]
            - data.loc[data["market_cap_twd"].notna(), "issued_shares"]
            * data.loc[data["market_cap_twd"].notna(), "close"]
        ).abs().max()
        structural = bool(
            quality.get("structural_passed")
            and data["issued_shares"].gt(0).all()
            and data["issued_shares"].eq(data["issued_shares"].astype("int64")).all()
            and no_lookahead
            and float(equation_error or 0) <= 1e-6
            and source_quality["minimum_monthly_issued_share_coverage"] == 1.0
        )
        exact_industry = bool(
            source_quality["industry_frequency"] == "monthly"
            and source_quality["maximum_industry_stale_days"] == 0
            and source_quality["exact_industry_snapshot_rows"] == len(data)
            and no_lookahead
        )
        blockers = []
        if not exact_industry:
            blockers.append(
                "industry membership is not observed on every monthly snapshot date"
            )
        if source_quality["industry_pit_coverage"] < 1.0:
            blockers.append(
                "some common-stock rows have no official MI_QFIIS industry category; "
                "these remain missing"
            )
        if source_quality["same_day_market_cap_coverage"] < 1.0:
            blockers.append(
                "same-day raw close is unavailable for a small set of suspended securities; "
                "market cap remains missing rather than forward-filled"
            )
        blockers.append("D3 corporate-action execution ledger is not promotion-ready")
        quality.update({
            "snapshot_id": args.snapshot_id,
            "scope": (
                "D4_TWSE_monthly_issued_shares_market_cap_"
                f"{source_quality['industry_frequency']}_industry_staging"
            ),
            "structural_passed": structural,
            "rows": int(len(data)),
            "stock_ids": int(data["stock_id"].astype(str).nunique()),
            "monthly_snapshots": int(data["snapshot_date"].nunique()),
            "issued_shares_unit": "shares",
            "nonpositive_issued_shares": int(data["issued_shares"].le(0).sum()),
            "nonintegral_issued_shares": int(
                data["issued_shares"].ne(data["issued_shares"].astype("int64")).sum()
            ),
            "minimum_monthly_issued_share_coverage": source_quality[
                "minimum_monthly_issued_share_coverage"
            ],
            "industry_pit_coverage": source_quality["industry_pit_coverage"],
            "industry_frequency": source_quality["industry_frequency"],
            "maximum_industry_stale_days": source_quality["maximum_industry_stale_days"],
            "same_day_market_cap_coverage": source_quality[
                "same_day_market_cap_coverage"
            ],
            "industry_observation_after_snapshot_rows": int(lookahead_rows.sum()),
            "maximum_market_cap_equation_error": float(equation_error or 0),
            "issued_shares_market_cap_ready": structural,
            "exact_industry_asof_ready": exact_industry,
            "d4_component_ready": bool(structural and exact_industry),
            "promotion_ready": False,
            "blockers": blockers,
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
