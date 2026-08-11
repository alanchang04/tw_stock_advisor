"""Versioned research-snapshot manifests and structural quality checks.

This module deliberately does not know how to download market data.  It freezes
and validates a directory of parquet files so acquisition and research remain
separate concerns.  The manifest is content-addressed: a backtest report can
name the exact file hashes it consumed instead of relying on a mutable path.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from typing import Any

import pandas as pd
import pyarrow.parquet as pq


SNAPSHOT_SCHEMA_VERSION = 1

PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "prices.parquet": ("stock_id", "trade_date"),
    "institutional.parquet": ("stock_id", "trade_date"),
    "margin.parquet": ("stock_id", "trade_date"),
    "monthly_revenue.parquet": ("stock_id", "year_month"),
    "dividend_events.parquet": ("stock_id", "ex_date"),
    "stocks.parquet": ("stock_id",),
    "delisted_stocks.parquet": ("stock_id",),
    "stock_universe_history.parquet": ("snapshot_date", "stock_id"),
    "stock_industry_map.parquet": ("stock_id", "industry_code"),
    "industries.parquet": ("industry_code",),
    "disposition_events.parquet": ("stock_id", "start_date"),
    "notice_events.parquet": ("stock_id", "notice_date", "reason"),
    "technical.parquet": ("stock_id", "trade_date"),
}

DATE_COLUMNS: dict[str, str] = {
    "prices.parquet": "trade_date",
    "institutional.parquet": "trade_date",
    "margin.parquet": "trade_date",
    "monthly_revenue.parquet": "year_month",
    "dividend_events.parquet": "ex_date",
    "stocks.parquet": "listing_date",
    "delisted_stocks.parquet": "delisting_date",
    "stock_universe_history.parquet": "snapshot_date",
    "disposition_events.parquet": "start_date",
    "notice_events.parquet": "notice_date",
    "technical.parquet": "trade_date",
}

CANONICAL_UNITS = {
    "prices.open/high/low/close": "TWD_per_share",
    "prices.volume": "shares",
    "prices.turnover": "TWD",
    "institutional.*_net": "shares",
    "margin.*_balance": "shares",
    "orders.shares": "shares_integer",
    "broker.common_lots": "lots_of_1000_shares",
    "broker.odd_lot_quantity": "shares",
}

CURRENT_REQUIRED_FILES = {
    "prices.parquet",
    "institutional.parquet",
    "margin.parquet",
    "monthly_revenue.parquet",
    "dividend_events.parquet",
    "stocks.parquet",
    "delisted_stocks.parquet",
    "stock_universe_history.parquet",
    "stock_industry_map.parquet",
    "industries.parquet",
    "disposition_events.parquet",
    "notice_events.parquet",
}


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest().upper()


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest().upper()


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def git_block(root: str | Path) -> dict[str, Any]:
    root = Path(root).resolve()
    status = _git(root, "status", "--porcelain=v1") or ""
    return {
        "commit": _git(root, "rev-parse", "HEAD"),
        "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status),
        "dirty_entries": status.splitlines(),
    }


def environment_block(root: str | Path) -> dict[str, Any]:
    packages = []
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name")
        if name:
            packages.append(f"{name}=={dist.version}")
    packages = sorted(set(packages), key=str.lower)
    requirements = Path(root) / "requirements.txt"
    return {
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "packages": packages,
        "packages_sha256": canonical_json_sha256(packages),
        "requirements_sha256": sha256_file(requirements) if requirements.exists() else None,
    }


def _null_counts_from_metadata(parquet: pq.ParquetFile) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    names = parquet.schema_arrow.names
    for column_index, name in enumerate(names):
        total = 0
        available = True
        for row_group_index in range(parquet.num_row_groups):
            stats = parquet.metadata.row_group(row_group_index).column(column_index).statistics
            if stats is None or stats.null_count is None:
                available = False
                break
            total += int(stats.null_count)
        result[name] = total if available else None
    return result


def profile_parquet(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    parquet = pq.ParquetFile(path)
    columns = parquet.schema_arrow.names
    key = tuple(c for c in PRIMARY_KEYS.get(path.name, ()) if c in columns)
    date_column = DATE_COLUMNS.get(path.name)
    selected = list(dict.fromkeys([*key, *([date_column] if date_column in columns else [])]))
    frame = pd.read_parquet(path, columns=selected) if selected else pd.DataFrame()
    duplicate_keys = int(frame.duplicated(list(key)).sum()) if key else None
    stock_count = int(frame["stock_id"].astype(str).nunique()) if "stock_id" in frame else None
    date_values = (
        frame[date_column].dropna().map(str)
        if date_column in frame and len(frame) else pd.Series(dtype="object")
    )
    date_min = str(date_values.min()) if len(date_values) else None
    date_max = str(date_values.max()) if len(date_values) else None
    return {
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "rows": parquet.metadata.num_rows,
        "row_groups": parquet.num_row_groups,
        "columns": columns,
        "schema": {field.name: str(field.type) for field in parquet.schema_arrow},
        "primary_key": list(key),
        "duplicate_primary_keys": duplicate_keys,
        "null_counts": _null_counts_from_metadata(parquet),
        "stock_count": stock_count,
        "date_column": date_column if date_column in columns else None,
        "date_min": date_min,
        "date_max": date_max,
    }


def build_manifest(snapshot_dir: str | Path, project_root: str | Path) -> dict[str, Any]:
    snapshot = Path(snapshot_dir).resolve()
    root = Path(project_root).resolve()
    files = {
        path.name: profile_parquet(path)
        for path in sorted(snapshot.glob("*.parquet"), key=lambda p: p.name.lower())
    }
    manifest = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": snapshot.name,
        "created_at": datetime.now().astimezone().isoformat(),
        "snapshot_dir": str(snapshot),
        "source_policy": "official_first_no_unreconciled_splicing",
        "units": CANONICAL_UNITS,
        "git": git_block(root),
        "environment": environment_block(root),
        "files": files,
    }
    manifest["content_sha256"] = canonical_json_sha256({
        name: value["sha256"] for name, value in files.items()
    })
    return manifest


def _price_quality(path: Path) -> dict[str, int]:
    parquet = pq.ParquetFile(path)
    counts = Counter({
        "nonpositive_open": 0,
        "nonpositive_high": 0,
        "nonpositive_low": 0,
        "nonpositive_close": 0,
        "negative_volume": 0,
        "negative_turnover": 0,
        "ohlc_inconsistent": 0,
    })
    columns = ["open", "high", "low", "close", "volume", "turnover"]
    if not set(columns).issubset(parquet.schema_arrow.names):
        return dict(counts)
    for batch in parquet.iter_batches(columns=columns, batch_size=250_000):
        frame = batch.to_pandas()
        for column in ("open", "high", "low", "close"):
            value = pd.to_numeric(frame[column], errors="coerce")
            counts[f"nonpositive_{column}"] += int((value.notna() & (value <= 0)).sum())
        volume = pd.to_numeric(frame["volume"], errors="coerce")
        turnover = pd.to_numeric(frame["turnover"], errors="coerce")
        counts["negative_volume"] += int((volume.notna() & (volume < 0)).sum())
        counts["negative_turnover"] += int((turnover.notna() & (turnover < 0)).sum())
        complete = frame[["open", "high", "low", "close"]].notna().all(axis=1)
        max_body = frame[["open", "close"]].max(axis=1)
        min_body = frame[["open", "close"]].min(axis=1)
        invalid = complete & (
            (frame["low"] > min_body) | (frame["high"] < max_body)
            | (frame["low"] > frame["high"])
        )
        counts["ohlc_inconsistent"] += int(invalid.sum())
    return dict(counts)


def build_quality_report(snapshot_dir: str | Path, manifest: dict[str, Any],
                         required_files: set[str] | None = None) -> dict[str, Any]:
    snapshot = Path(snapshot_dir).resolve()
    files = manifest["files"]
    errors: list[str] = []
    warnings: list[str] = []
    expected_files = CURRENT_REQUIRED_FILES if required_files is None else set(required_files)
    full_research_scope = expected_files == CURRENT_REQUIRED_FILES
    missing = sorted(expected_files - set(files))
    if missing:
        errors.append("missing required parquet files: " + ", ".join(missing))
    duplicates = {
        name: profile["duplicate_primary_keys"]
        for name, profile in files.items()
        if profile["duplicate_primary_keys"] not in (None, 0)
    }
    if duplicates:
        errors.append(f"duplicate primary keys: {duplicates}")

    price_checks = _price_quality(snapshot / "prices.parquet") if "prices.parquet" in files else {}
    if any(price_checks.values()):
        errors.append(f"invalid price rows: {price_checks}")

    universe_snapshots = None
    universe_path = snapshot / "stock_universe_history.parquet"
    if universe_path.exists():
        universe = pd.read_parquet(universe_path, columns=["snapshot_date", "market"])
        universe_snapshots = int(universe["snapshot_date"].nunique())
        if universe_snapshots < 2:
            warnings.append("universe history has fewer than 2 snapshots; it is not point-in-time history")

    institutional_path = snapshot / "institutional.parquet"
    stocks_path = snapshot / "stocks.parquet"
    tpex_pre2018_rows = None
    if institutional_path.exists() and stocks_path.exists():
        inst = pd.read_parquet(institutional_path, columns=["stock_id", "trade_date"])
        stocks = pd.read_parquet(stocks_path, columns=["stock_id", "market"])
        market = stocks.assign(stock_id=stocks["stock_id"].astype(str)).drop_duplicates("stock_id")
        inst["stock_id"] = inst["stock_id"].astype(str)
        inst["trade_date"] = pd.to_datetime(inst["trade_date"], errors="coerce")
        inst = inst.merge(market, on="stock_id", how="left")
        tpex_pre2018_rows = int(
            ((inst["market"] == "TPEX") & (inst["trade_date"] < pd.Timestamp("2018-01-01"))).sum()
        )
        if tpex_pre2018_rows == 0:
            warnings.append("TPEX institutional data is absent before 2018; all-market eras are incomparable")

    for event_name in ("disposition_events.parquet", "notice_events.parquet"):
        event_path = snapshot / event_name
        if event_path.exists() and "market" in pq.ParquetFile(event_path).schema_arrow.names:
            markets = set(pd.read_parquet(event_path, columns=["market"])["market"].dropna())
            if "TPEX" not in markets:
                warnings.append(f"{event_name} has no TPEX events")

    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "snapshot_id": manifest["snapshot_id"],
        "content_sha256": manifest["content_sha256"],
        "structural_passed": not errors,
        "research_ready_for_point_in_time_all_market": (
            full_research_scope and not errors and not warnings
        ),
        "errors": errors,
        "warnings": warnings,
        "checks": {
            "required_file_count": len(expected_files),
            "observed_parquet_count": len(files),
            "full_research_scope": full_research_scope,
            "duplicate_primary_keys": duplicates,
            "price_checks": price_checks,
            "universe_snapshot_count": universe_snapshots,
            "tpex_institutional_rows_before_2018": tpex_pre2018_rows,
        },
    }


def write_json(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False, default=str)
        handle.write("\n")
