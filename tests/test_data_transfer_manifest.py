import json
from pathlib import Path

from scripts.build_data_transfer_manifest import (
    build_manifest,
    verify_manifest,
    write_manifest,
)


def test_transfer_manifest_round_trip_and_detects_corruption(tmp_path: Path):
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    (raw / "a.json.gz").write_bytes(b"official-a")
    (raw / "b.json.gz").write_bytes(b"official-b")

    manifest = build_manifest(tmp_path, [raw])
    output = tmp_path / "transfer.json"
    write_manifest(output, manifest)
    loaded = json.loads(output.read_text(encoding="utf-8"))

    first = verify_manifest(tmp_path, loaded)
    assert first["passed"]
    assert first["checked_files"] == 2

    (raw / "b.json.gz").write_bytes(b"corrupted")
    second = verify_manifest(tmp_path, loaded)
    assert not second["passed"]
    assert [item["path"] for item in second["mismatched"]] == [
        "data/raw/b.json.gz"
    ]
