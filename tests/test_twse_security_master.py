import gzip
import json
import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd

from scripts.build_twse_security_master import (
    _atomic_write,
    _classify_asset,
    build_staging_snapshot,
)


def test_atomic_write_replaces_complete_content(tmp_path: Path):
    target = tmp_path / "source.bin"
    _atomic_write(target, b"first")
    _atomic_write(target, b"second")
    assert target.read_bytes() == b"second"
    assert not list(tmp_path.glob("*.tmp-*"))


def test_asset_classification_uses_official_membership_not_code_shape():
    assert _classify_asset("2330", {"產業別": "24", "公司簡稱": "台積電"}, None)[0] == "common_stock"
    assert _classify_asset("9103", {"產業別": "91", "公司簡稱": "美德醫療-DR"}, None)[0] == "depositary_receipt"
    assert _classify_asset("9101", None, {"Company": "福雷電"})[0] == "depositary_receipt"
    assert _classify_asset("0050", None, None)[0] == "non_company_security"


def test_staging_snapshot_excludes_observations_after_stage_end(tmp_path: Path):
    db_path = tmp_path / "observations.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE security_observations (
                trade_date TEXT NOT NULL,
                stock_id TEXT NOT NULL,
                stock_name TEXT NOT NULL
            );
            CREATE TABLE daily_prices (
                trade_date TEXT NOT NULL,
                stock_id TEXT NOT NULL
            );
            INSERT INTO security_observations VALUES
                ('2007-12-28', '0050', 'ETF'),
                ('2008-01-02', '9999', 'Future issuer');
            INSERT INTO daily_prices VALUES
                ('2007-12-28', '0050'),
                ('2008-01-02', '9999');
            """
        )
        connection.commit()
    finally:
        connection.close()

    as_of = date(2026, 8, 11)
    raw_root = tmp_path / "raw"
    official_json = raw_root / "official_json"
    official_json.mkdir(parents=True)
    for name in ("current_companies", "delisted_companies"):
        with gzip.open(official_json / f"{name}_{as_of}.json.gz", "wt", encoding="utf-8") as handle:
            json.dump([], handle)
    (raw_root / f"source_manifest_{as_of}.json").write_text("{}", encoding="utf-8")

    target = build_staging_snapshot(
        db_path=db_path,
        raw_root=raw_root,
        output_root=tmp_path / "versions",
        snapshot_id="bounded",
        as_of=as_of,
        stage_start=date(2005, 1, 1),
        stage_end=date(2007, 12, 31),
    )

    master = pd.read_parquet(target / "security_master_staging.parquet")
    history = pd.read_parquet(target / "stock_universe_history.parquet")
    assert master["stock_id"].tolist() == ["0050"]
    assert history.empty
