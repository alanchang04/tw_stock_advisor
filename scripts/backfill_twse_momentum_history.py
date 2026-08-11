"""Archive and normalize TWSE daily prices for the MOM-1 research snapshot.

Safety properties:
- never writes to ``data/research`` or Neon;
- stores every official JSON response in a gzip raw cache;
- resumes by date from an isolated SQLite database;
- exports through a temporary directory and refuses to overwrite a snapshot.

The downloader intentionally fetches prices only.  Historical universe and
corporate actions are separate work packages and must pass their own gates
before MOM-1 performance is opened.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import gzip
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import uuid

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data_pipeline.fetchers.twse_fetcher import (  # noqa: E402
    URL_TWSE_PRICE_BYDATE, _get_json, parse_prices_twse_by_date_payload,
)
from research.snapshot_manifest import (  # noqa: E402
    build_manifest, build_quality_report, canonical_json_sha256, sha256_file, write_json,
)


PRICE_COLUMNS = [
    "stock_id", "trade_date", "open", "high", "low", "close",
    "volume", "turnover", "change_pct",
]


def raw_path(raw_root: str | Path, trade_date: date) -> Path:
    return Path(raw_root) / f"{trade_date.year:04d}" / f"MI_INDEX_{trade_date:%Y%m%d}.json.gz"


def write_raw_response(path: str | Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    try:
        with gzip.open(temporary, "wt", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_raw_response(path: str | Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS daily_prices (
            stock_id TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, turnover REAL, change_pct REAL,
            PRIMARY KEY (stock_id, trade_date)
        );
        CREATE TABLE IF NOT EXISTS fetch_status (
            trade_date TEXT PRIMARY KEY,
            source_status TEXT NOT NULL,
            rows INTEGER NOT NULL,
            raw_path TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS security_observations (
            stock_id TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            stock_name TEXT NOT NULL,
            PRIMARY KEY (stock_id, trade_date)
        );
        CREATE TABLE IF NOT EXISTS security_observation_status (
            trade_date TEXT PRIMARY KEY,
            rows INTEGER NOT NULL
        );
    """)
    connection.commit()
    return connection


def upsert_day(connection: sqlite3.Connection, trade_date: date, frame: pd.DataFrame,
               source_status: str, archive_path: Path) -> None:
    if not frame.empty:
        normalized = frame[PRICE_COLUMNS].copy()
        normalized["stock_id"] = normalized["stock_id"].astype(str)
        normalized["trade_date"] = normalized["trade_date"].astype(str)
        connection.executemany(
            """INSERT INTO daily_prices
               (stock_id, trade_date, open, high, low, close, volume, turnover, change_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(stock_id, trade_date) DO UPDATE SET
                 open=excluded.open, high=excluded.high, low=excluded.low,
                 close=excluded.close, volume=excluded.volume,
                 turnover=excluded.turnover, change_pct=excluded.change_pct""",
            normalized.itertuples(index=False, name=None),
        )
        if "stock_name" in frame.columns:
            observations = frame[["stock_id", "trade_date", "stock_name"]].copy()
            observations["stock_id"] = observations["stock_id"].astype(str)
            observations["trade_date"] = observations["trade_date"].astype(str)
            observations["stock_name"] = observations["stock_name"].fillna("").astype(str)
            connection.executemany(
                """INSERT INTO security_observations (stock_id, trade_date, stock_name)
                   VALUES (?, ?, ?)
                   ON CONFLICT(stock_id, trade_date) DO UPDATE SET
                     stock_name=excluded.stock_name""",
                observations.itertuples(index=False, name=None),
            )
    connection.execute(
        """INSERT INTO security_observation_status (trade_date, rows)
           VALUES (?, ?)
           ON CONFLICT(trade_date) DO UPDATE SET rows=excluded.rows""",
        (trade_date.isoformat(), len(frame)),
    )
    connection.execute(
        """INSERT INTO fetch_status
           (trade_date, source_status, rows, raw_path, raw_sha256, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(trade_date) DO UPDATE SET
             source_status=excluded.source_status, rows=excluded.rows,
             raw_path=excluded.raw_path, raw_sha256=excluded.raw_sha256,
             fetched_at=excluded.fetched_at""",
        (trade_date.isoformat(), source_status, len(frame), str(archive_path.resolve()),
         sha256_file(archive_path), datetime.now().astimezone().isoformat()),
    )
    connection.commit()


def weekday_dates(start: date, end: date):
    current = start
    while current <= end:
        if current.weekday() < 5:
            yield current
        current += timedelta(days=1)


def fetch_range(start: date, end: date, raw_root: str | Path,
                db_path: str | Path, delay: float = 0.8) -> dict:
    connection = connect(db_path)
    downloaded = cached = trading_days = rows = 0
    try:
        completed = {
            row[0] for row in connection.execute("SELECT trade_date FROM fetch_status")
        }
        observations_completed = {
            row[0] for row in connection.execute(
                "SELECT trade_date FROM security_observation_status"
            )
        }
        for index, trade_date in enumerate(weekday_dates(start, end), start=1):
            iso_date = trade_date.isoformat()
            if iso_date in completed and iso_date in observations_completed:
                cached += 1
                continue
            archive = raw_path(raw_root, trade_date)
            if archive.exists():
                payload = read_raw_response(archive)
                cached += 1
            else:
                payload = _get_json(URL_TWSE_PRICE_BYDATE, params={
                    "date": trade_date.strftime("%Y%m%d"),
                    "type": "ALLBUT0999",
                    "response": "json",
                })
                write_raw_response(archive, payload)
                downloaded += 1
                if delay:
                    time.sleep(delay)
            frame = parse_prices_twse_by_date_payload(payload, trade_date)
            status = str(payload.get("stat", "UNKNOWN"))
            upsert_day(connection, trade_date, frame, status, archive)
            rows += len(frame)
            trading_days += int(not frame.empty)
            if index % 20 == 0:
                print(
                    f"progress date={trade_date} downloaded={downloaded} cached={cached} "
                    f"trading_days={trading_days} rows={rows}", flush=True,
                )
    finally:
        connection.close()
    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "downloaded": downloaded, "cached": cached,
        "trading_days": trading_days, "parsed_rows_this_run": rows,
    }


def summarize_database(db_path: str | Path) -> dict:
    """Return cumulative, read-only progress and unit-integrity checks."""
    connection = connect(db_path)
    try:
        status = connection.execute(
            "SELECT COUNT(*), SUM(rows > 0), COALESCE(SUM(rows), 0), "
            "MIN(trade_date), MAX(trade_date) FROM fetch_status"
        ).fetchone()
        prices = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT stock_id), MIN(trade_date), MAX(trade_date), "
            "SUM(volume < 0), SUM(volume != CAST(volume AS INTEGER)) FROM daily_prices"
        ).fetchone()
        observations = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT stock_id), COUNT(DISTINCT trade_date) "
            "FROM security_observations"
        ).fetchone()
    finally:
        connection.close()
    return {
        "db": str(Path(db_path).resolve()),
        "completed_weekdays": status[0],
        "trading_days": status[1] or 0,
        "status_declared_rows": status[2],
        "price_rows": prices[0],
        "stock_ids": prices[1],
        "status_date_min": status[3],
        "status_date_max": status[4],
        "price_date_min": prices[2],
        "price_date_max": prices[3],
        "negative_volume_rows": prices[4] or 0,
        "non_integral_volume_rows": prices[5] or 0,
        "security_observation_rows": observations[0],
        "security_observation_stock_ids": observations[1],
        "security_observation_dates": observations[2],
        "volume_unit": "shares",
        "row_counts_match": status[2] == prices[0],
    }


def export_snapshot(db_path: str | Path, raw_root: str | Path,
                    output_root: str | Path, snapshot_id: str,
                    date_start: date, date_end: date) -> Path:
    output_root = Path(output_root).resolve()
    target = output_root / snapshot_id
    if target.exists():
        raise FileExistsError(f"snapshot already exists and will not be overwritten: {target}")
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = output_root / f".{snapshot_id}.tmp-{uuid.uuid4().hex}"
    temporary.mkdir()
    try:
        connection = sqlite3.connect(db_path)
        try:
            prices = pd.read_sql_query(
                "SELECT stock_id, trade_date, open, high, low, close, volume, turnover, "
                "change_pct FROM daily_prices "
                "WHERE trade_date BETWEEN :start AND :end "
                "ORDER BY trade_date, stock_id",
                connection,
                params={"start": date_start.isoformat(), "end": date_end.isoformat()},
            )
            status = pd.read_sql_query(
                "SELECT trade_date, source_status, rows, raw_path, raw_sha256, fetched_at "
                "FROM fetch_status WHERE trade_date BETWEEN :start AND :end "
                "ORDER BY trade_date",
                connection,
                params={"start": date_start.isoformat(), "end": date_end.isoformat()},
            )
        finally:
            connection.close()
        prices.to_parquet(temporary / "prices.parquet", index=False)
        manifest = build_manifest(temporary, ROOT)
        manifest["snapshot_id"] = snapshot_id
        manifest["snapshot_dir"] = str(target)
        manifest["source"] = {
            "provider": "TWSE",
            "endpoint": URL_TWSE_PRICE_BYDATE,
            "raw_root": str(Path(raw_root).resolve()),
            "raw_response_count": len(status),
            "raw_responses_sha256": canonical_json_sha256(
                status[["trade_date", "raw_sha256"]].to_dict("records")
            ),
            "requested_date_start": date_start.isoformat(),
            "requested_date_end": date_end.isoformat(),
            "date_min": str(status["trade_date"].min()) if len(status) else None,
            "date_max": str(status["trade_date"].max()) if len(status) else None,
        }
        quality = build_quality_report(temporary, manifest, required_files={"prices.parquet"})
        quality["snapshot_id"] = snapshot_id
        quality["scope"] = "D2_price_only_not_ready_for_strategy_backtest"
        write_json(temporary / "manifest.json", manifest)
        write_json(temporary / "quality_report.json", quality)
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def parse_iso_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=parse_iso_date, default=date(2005, 1, 1))
    parser.add_argument("--end", type=parse_iso_date, default=date(2014, 12, 31))
    parser.add_argument("--raw-root", default=str(ROOT / "data" / "raw" / "twse" / "mi_index"))
    parser.add_argument("--db", default=str(ROOT / "data" / "raw" / "twse" / "mi_index.sqlite3"))
    parser.add_argument("--delay", type=float, default=0.8)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--output-root", default=str(ROOT / "data" / "research_versions"))
    parser.add_argument("--snapshot-id", default="twse_prices_2005_2014_v1")
    args = parser.parse_args()
    if args.status:
        print(json.dumps(summarize_database(args.db), ensure_ascii=False, indent=2))
        return
    if args.end < args.start:
        parser.error("--end must not be earlier than --start")
    result = fetch_range(args.start, args.end, args.raw_root, args.db, max(0.0, args.delay))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if args.export:
        print(export_snapshot(
            args.db, args.raw_root, args.output_root, args.snapshot_id,
            args.start, args.end,
        ))


if __name__ == "__main__":
    main()
