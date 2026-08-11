from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from research.snapshot_manifest import (
    build_manifest, build_quality_report, canonical_json_sha256, sha256_file,
)
from scripts.build_research_snapshot_v2 import create_snapshot


def _write_minimal_snapshot(path: Path) -> None:
    path.mkdir(parents=True)
    pd.DataFrame([
        {"stock_id": "2330", "trade_date": "2020-01-02", "open": 10.0,
         "high": 11.0, "low": 9.0, "close": 10.5, "volume": 1000,
         "turnover": 10500, "change_pct": 5.0},
        {"stock_id": "2330", "trade_date": "2020-01-03", "open": 10.5,
         "high": 12.0, "low": 10.0, "close": 11.0, "volume": 1200,
         "turnover": 13200, "change_pct": 4.76},
    ]).to_parquet(path / "prices.parquet", index=False)
    pd.DataFrame([
        {"snapshot_date": "2020-01-02", "stock_id": "2330", "market": "TWSE"},
        {"snapshot_date": "2020-01-03", "stock_id": "2330", "market": "TWSE"},
    ]).to_parquet(path / "stock_universe_history.parquet", index=False)


def test_hash_helpers_are_deterministic(tmp_path: Path):
    path = tmp_path / "x.bin"
    path.write_bytes(b"abc")
    assert sha256_file(path) == (
        "BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A9CB410FF61F20015AD"
    )
    assert canonical_json_sha256({"b": 1, "a": 2}) == canonical_json_sha256({"a": 2, "b": 1})


def test_manifest_profiles_keys_dates_and_units(tmp_path: Path):
    source = tmp_path / "source"
    _write_minimal_snapshot(source)
    manifest = build_manifest(source, Path(__file__).resolve().parents[1])
    prices = manifest["files"]["prices.parquet"]
    assert prices["rows"] == 2
    assert prices["duplicate_primary_keys"] == 0
    assert prices["date_min"].startswith("2020-01-02")
    assert manifest["units"]["prices.volume"] == "shares"
    assert len(manifest["content_sha256"]) == 64


def test_manifest_date_bounds_ignore_nulls_and_mixed_storage(tmp_path: Path):
    source = tmp_path / "source"
    _write_minimal_snapshot(source)
    pd.DataFrame([
        {"stock_id": "A", "listing_date": pd.Timestamp("2020-01-01").date(), "market": "TWSE"},
        {"stock_id": "B", "listing_date": None, "market": "TWSE"},
    ]).to_parquet(source / "stocks.parquet", index=False)
    manifest = build_manifest(source, Path(__file__).resolve().parents[1])
    profile = manifest["files"]["stocks.parquet"]
    assert profile["date_min"] == "2020-01-01"
    assert profile["date_max"] == "2020-01-01"


def test_quality_report_flags_missing_files_without_hiding_price_checks(tmp_path: Path):
    source = tmp_path / "source"
    _write_minimal_snapshot(source)
    manifest = build_manifest(source, Path(__file__).resolve().parents[1])
    quality = build_quality_report(source, manifest)
    assert not quality["structural_passed"]
    assert quality["checks"]["price_checks"]["ohlc_inconsistent"] == 0
    assert any("missing required" in message for message in quality["errors"])


def test_price_only_snapshot_is_structural_but_not_full_research_ready(tmp_path: Path):
    source = tmp_path / "source"
    _write_minimal_snapshot(source)
    manifest = build_manifest(source, Path(__file__).resolve().parents[1])
    quality = build_quality_report(source, manifest, required_files={"prices.parquet"})
    assert quality["structural_passed"]
    assert not quality["research_ready_for_point_in_time_all_market"]
    assert not quality["checks"]["full_research_scope"]


def test_snapshot_builder_copies_parquet_and_refuses_overwrite(tmp_path: Path):
    source = tmp_path / "source"
    output = tmp_path / "versions"
    _write_minimal_snapshot(source)
    target = create_snapshot(source, output, "research_v_test",
                             project_root=Path(__file__).resolve().parents[1])
    assert (target / "manifest.json").exists()
    assert sha256_file(target / "prices.parquet") == sha256_file(source / "prices.parquet")
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["snapshot_id"] == "research_v_test"
    with pytest.raises(FileExistsError):
        create_snapshot(source, output, "research_v_test",
                        project_root=Path(__file__).resolve().parents[1])
