from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from data_pipeline.fetchers.twse_fetcher import parse_prices_twse_by_date_payload
from scripts.backfill_twse_momentum_history import (
    connect, export_snapshot, raw_path, read_raw_response, summarize_database, upsert_day,
    write_raw_response,
)


def _payload():
    row = ["2330", "台積電", "1,000", "1", "600,000", "600", "610", "595",
           "605", '<p style="color:red">+</p>', "5"]
    return {
        "stat": "OK",
        "tables": [{"title": "每日收盤行情(全部)", "data": [row]}],
    }


def test_archived_payload_round_trip_and_parser(tmp_path: Path):
    day = date(2020, 1, 2)
    archive = raw_path(tmp_path, day)
    write_raw_response(archive, _payload())
    loaded = read_raw_response(archive)
    frame = parse_prices_twse_by_date_payload(loaded, day)
    assert len(frame) == 1
    assert frame.iloc[0]["stock_id"] == "2330"
    assert frame.iloc[0]["stock_name"]
    assert frame.iloc[0]["volume"] == 1000
    assert frame.iloc[0]["close"] == 605


def test_upsert_day_is_resumable_and_keeps_share_units(tmp_path: Path):
    day = date(2020, 1, 2)
    archive = raw_path(tmp_path / "raw", day)
    write_raw_response(archive, _payload())
    frame = parse_prices_twse_by_date_payload(read_raw_response(archive), day)
    connection = connect(tmp_path / "history.sqlite3")
    try:
        upsert_day(connection, day, frame, "OK", archive)
        upsert_day(connection, day, frame, "OK", archive)
        row = connection.execute(
            "SELECT stock_id, volume, COUNT(*) FROM daily_prices GROUP BY stock_id, volume"
        ).fetchone()
        status = connection.execute("SELECT COUNT(*) FROM fetch_status").fetchone()[0]
        observation = connection.execute(
            "SELECT stock_id, stock_name FROM security_observations"
        ).fetchone()
    finally:
        connection.close()
    assert row == ("2330", 1000.0, 1)
    assert status == 1
    assert observation[0] == "2330"
    summary = summarize_database(tmp_path / "history.sqlite3")
    assert summary["completed_weekdays"] == 1
    assert summary["price_rows"] == 1
    assert summary["row_counts_match"]
    assert summary["non_integral_volume_rows"] == 0
    assert summary["volume_unit"] == "shares"
    assert summary["security_observation_rows"] == 1


def test_export_snapshot_respects_requested_date_boundary(tmp_path: Path):
    raw_root = tmp_path / "raw"
    db_path = tmp_path / "history.sqlite3"
    connection = connect(db_path)
    try:
        for day in (date(2020, 1, 2), date(2020, 1, 3)):
            archive = raw_path(raw_root, day)
            write_raw_response(archive, _payload())
            frame = parse_prices_twse_by_date_payload(read_raw_response(archive), day)
            upsert_day(connection, day, frame, "OK", archive)
    finally:
        connection.close()

    target = export_snapshot(
        db_path,
        raw_root,
        tmp_path / "versions",
        "bounded",
        date(2020, 1, 2),
        date(2020, 1, 2),
    )

    prices = pd.read_parquet(target / "prices.parquet")
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert prices["trade_date"].tolist() == ["2020-01-02"]
    assert manifest["source"]["raw_response_count"] == 1
    assert manifest["source"]["date_max"] == "2020-01-02"
