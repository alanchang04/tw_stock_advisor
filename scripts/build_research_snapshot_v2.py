"""Create an immutable, content-addressed research snapshot from parquet files.

The source directory is never modified.  Files are copied to a temporary
directory, verified by SHA256, profiled, and atomically renamed only after the
manifest and structural quality report have been written.

Example:
    python scripts/build_research_snapshot_v2.py \
        --source data/research \
        --snapshot-id research_v20260811_current_bf58807
"""
from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.snapshot_manifest import (  # noqa: E402
    build_manifest, build_quality_report, sha256_file, write_json,
)


def default_snapshot_id() -> str:
    return "research_v" + datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")


def create_snapshot(source: str | Path, output_root: str | Path,
                    snapshot_id: str, project_root: str | Path = ROOT) -> Path:
    source = Path(source).resolve()
    output_root = Path(output_root).resolve()
    target = output_root / snapshot_id
    if not source.is_dir():
        raise FileNotFoundError(f"source snapshot directory not found: {source}")
    if target.exists():
        raise FileExistsError(f"snapshot already exists and will not be overwritten: {target}")
    parquet_files = sorted(source.glob("*.parquet"), key=lambda path: path.name.lower())
    if not parquet_files:
        raise ValueError(f"source contains no parquet files: {source}")

    output_root.mkdir(parents=True, exist_ok=True)
    temporary = output_root / f".{snapshot_id}.tmp-{uuid.uuid4().hex}"
    temporary.mkdir()
    try:
        for source_path in parquet_files:
            destination = temporary / source_path.name
            shutil.copy2(source_path, destination)
            if sha256_file(source_path) != sha256_file(destination):
                raise OSError(f"copy hash mismatch: {source_path.name}")

        manifest = build_manifest(temporary, project_root)
        # The temporary name must not become the durable snapshot identity.
        manifest["snapshot_id"] = snapshot_id
        manifest["snapshot_dir"] = str(target)
        quality = build_quality_report(temporary, manifest)
        quality["snapshot_id"] = snapshot_id
        write_json(temporary / "manifest.json", manifest)
        write_json(temporary / "quality_report.json", quality)
        readme = (
            f"# {snapshot_id}\n\n"
            f"Created from `{source}`.\n\n"
            f"Content SHA256: `{manifest['content_sha256']}`.\n\n"
            "This directory is immutable. Create a new snapshot instead of editing it.\n"
        )
        (temporary / "README.md").write_text(readme, encoding="utf-8", newline="\n")
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(ROOT / "data" / "research"))
    parser.add_argument("--output-root", default=str(ROOT / "data" / "research_versions"))
    parser.add_argument("--snapshot-id", default=default_snapshot_id())
    args = parser.parse_args()
    target = create_snapshot(args.source, args.output_root, args.snapshot_id)
    print(target)


if __name__ == "__main__":
    main()
