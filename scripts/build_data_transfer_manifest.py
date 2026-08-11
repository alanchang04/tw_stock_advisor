"""Create or verify a per-file checksum manifest for cloud data transfer."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def collection_sha256(files: list[dict]) -> str:
    payload = json.dumps(
        files, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest().upper()


def build_manifest(base: Path, roots: list[Path]) -> dict:
    base = base.resolve()
    resolved_roots = [root.resolve() for root in roots]
    records: list[dict] = []
    for root in resolved_roots:
        root.relative_to(base)
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(root)
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.resolve().relative_to(base).as_posix()
            records.append({
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            })
    records.sort(key=lambda item: item["path"])
    return {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(),
        "base_dir_contract": "repository_root",
        "roots": [root.relative_to(base).as_posix() for root in resolved_roots],
        "file_count": len(records),
        "total_bytes": sum(item["bytes"] for item in records),
        "files": records,
        "collection_sha256": collection_sha256(records),
    }


def write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def verify_manifest(base: Path, manifest: dict) -> dict:
    base = base.resolve()
    missing: list[str] = []
    mismatched: list[dict] = []
    for expected in manifest["files"]:
        path = (base / expected["path"]).resolve()
        path.relative_to(base)
        if not path.is_file():
            missing.append(expected["path"])
            continue
        actual_size = path.stat().st_size
        actual_sha = sha256_file(path)
        if actual_size != expected["bytes"] or actual_sha != expected["sha256"]:
            mismatched.append({
                "path": expected["path"],
                "expected_bytes": expected["bytes"],
                "actual_bytes": actual_size,
                "expected_sha256": expected["sha256"],
                "actual_sha256": actual_sha,
            })
    declared_collection = collection_sha256(manifest["files"])
    collection_matches = declared_collection == manifest["collection_sha256"]
    return {
        "passed": not missing and not mismatched and collection_matches,
        "checked_files": len(manifest["files"]),
        "missing": missing,
        "mismatched": mismatched,
        "collection_manifest_matches": collection_matches,
        "collection_sha256": declared_collection,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--base", type=Path, default=ROOT)
    create.add_argument("--root", action="append", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--base", type=Path, default=ROOT)
    verify.add_argument("--manifest", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "create":
        roots = [path if path.is_absolute() else args.base / path for path in args.root]
        manifest = build_manifest(args.base, roots)
        output = args.output if args.output.is_absolute() else args.base / args.output
        write_manifest(output, manifest)
        print(json.dumps({
            "manifest": str(output.resolve()),
            "file_count": manifest["file_count"],
            "total_bytes": manifest["total_bytes"],
            "collection_sha256": manifest["collection_sha256"],
        }, ensure_ascii=False, indent=2))
        return

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = verify_manifest(args.base, manifest)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
