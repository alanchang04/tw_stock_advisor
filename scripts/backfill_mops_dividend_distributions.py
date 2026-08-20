"""Archive and normalize MOPS dividend-distribution aggregate tables."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.mops_dividend_distribution import parse_mops_dividend_html
from research.snapshot_manifest import build_manifest, build_quality_report, write_json


URL = "https://mopsov.twse.com.tw/server-java/t05st09sub"


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest().upper()


def archive_path(raw_root: Path, roc_year: int) -> Path:
    return raw_root / str(roc_year + 1911) / f"t05st09sub_sii_{roc_year}_qryType1.html.gz"


def read_archive(path: Path) -> bytes:
    return gzip.decompress(path.read_bytes())


def write_archive(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))


def fetch_year(roc_year: int) -> bytes:
    query = urllib.parse.urlencode({
        "TYPEK": "sii", "YEAR": roc_year, "qryType": 1, "step": 1,
    })
    request = urllib.request.Request(
        f"{URL}?{query}", headers={"User-Agent": "tw-stock-advisor-research/1.0"}
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-roc-year", type=int, default=93)
    parser.add_argument("--end-roc-year", type=int, default=103)
    parser.add_argument(
        "--raw-root", type=Path,
        default=ROOT / "data" / "raw" / "mops" / "dividend_distribution",
    )
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "data" / "research" / "mops_dividend_distributions_2004_2014.parquet",
    )
    parser.add_argument("--cached-only", action="store_true")
    parser.add_argument("--snapshot-id")
    parser.add_argument(
        "--output-root", type=Path,
        default=ROOT / "data" / "research_versions",
    )
    args = parser.parse_args()
    if args.end_roc_year < args.start_roc_year:
        parser.error("--end-roc-year must not be earlier than --start-roc-year")

    frames = []
    source_status = []
    for roc_year in range(args.start_roc_year, args.end_roc_year + 1):
        path = archive_path(args.raw_root, roc_year)
        if path.exists():
            raw = read_archive(path)
            source = "cache"
        elif args.cached_only:
            raise FileNotFoundError(path)
        else:
            raw = fetch_year(roc_year)
            write_archive(path, raw)
            source = "download"
        frame = parse_mops_dividend_html(raw, roc_year)
        raw_sha = sha256_bytes(raw)
        frame["raw_path"] = path.resolve().relative_to(ROOT.resolve()).as_posix()
        frame["raw_sha256"] = raw_sha
        frames.append(frame)
        source_status.append({
            "distribution_year_roc": roc_year,
            "distribution_year": roc_year + 1911,
            "rows": len(frame),
            "raw_path": path.resolve().relative_to(ROOT.resolve()).as_posix(),
            "raw_sha256": raw_sha,
            "source": source,
            "query_type": "board_resolution_distribution_year",
        })
        print(f"roc_year={roc_year} rows={len(frame)} source={source}", flush=True)

    combined = pd.concat(frames, ignore_index=True)
    if args.snapshot_id:
        target = args.output_root / args.snapshot_id
        if target.exists():
            raise FileExistsError(f"snapshot already exists and will not be overwritten: {target}")
        args.output_root.mkdir(parents=True, exist_ok=True)
        temporary = args.output_root / f".{args.snapshot_id}.tmp-{uuid.uuid4().hex}"
        temporary.mkdir()
        try:
            combined.to_parquet(temporary / "dividend_terms.parquet", index=False)
            stable_status = pd.DataFrame(source_status).drop(columns=["source"])
            stable_status.to_parquet(temporary / "source_status.parquet", index=False)
            manifest = build_manifest(temporary, ROOT)
            manifest.update({
                "snapshot_id": args.snapshot_id,
                "snapshot_dir": target.resolve().relative_to(ROOT.resolve()).as_posix(),
                "source": {
                    "provider": "MOPS",
                    "endpoint": URL,
                    "query_type": 1,
                    "query_semantics": "board_resolution_distribution_year",
                    "roc_year_start": args.start_roc_year,
                    "roc_year_end": args.end_roc_year,
                    "raw_response_count": len(source_status),
                },
            })
            quality = build_quality_report(
                temporary, manifest,
                required_files={"dividend_terms.parquet", "source_status.parquet"},
            )
            quality.update({
                "snapshot_id": args.snapshot_id,
                "scope": "D3_MOPS_declared_dividend_terms",
                "rows": len(combined),
                "stock_ids": int(combined["stock_id"].nunique()),
                "negative_cash_terms": int(combined["cash_per_old_share"].lt(0).sum()),
                "invalid_share_multipliers": int(
                    combined["free_share_multiplier"].lt(1).sum()
                ),
                "structural_passed": bool(
                    quality.get("structural_passed")
                    and combined["cash_per_old_share"].ge(0).all()
                    and combined["free_share_multiplier"].ge(1).all()
                ),
                "promotion_ready": False,
                "blockers": [
                    "paid subscription and capital-reduction terms are outside this source",
                    "TWSE event matching must remain unique before ledger execution",
                ],
            })
            write_json(temporary / "manifest.json", manifest)
            write_json(temporary / "quality_report.json", quality)
            os.replace(temporary, target)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        print(json.dumps({
            "rows": len(combined), "snapshot": str(target),
            "content_sha256": manifest["content_sha256"],
        }, ensure_ascii=False))
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(args.output, index=False)
    status_path = args.output.with_name(f"{args.output.stem}_source_status.json")
    status_path.write_text(
        json.dumps({
            "endpoint": URL,
            "query_type": 1,
            "query_semantics": "board_resolution_distribution_year",
            "rows": len(combined),
            "years": source_status,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"rows": len(combined), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
