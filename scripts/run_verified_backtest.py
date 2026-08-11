"""Run the swing backtest from a frozen snapshot and save reproducible metrics."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.backtest import run_backtest
from agent.strategy import STRATEGY
from research.snapshot_manifest import canonical_json_sha256, sha256_file


def _yearly(nav: dict) -> dict[str, float]:
    series = pd.Series(nav, dtype=float)
    series.index = pd.to_datetime(series.index)
    year_end = series.resample("YE").last()
    previous = float(series.iloc[0])
    result = {}
    for stamp, value in year_end.items():
        result[str(stamp.year)] = float(value / previous - 1)
        previous = float(value)
    return result


def _jsonable(value):
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _frame_sha256(frame: pd.DataFrame) -> str:
    payload = frame.to_json(
        orient="records", date_format="iso", date_unit="us", double_precision=15,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest().upper()


def _nav_sha256(nav: dict) -> str:
    ordered = [(str(key), float(value)) for key, value in sorted(nav.items(), key=lambda x: x[0])]
    return canonical_json_sha256(ordered)


def build_report(snapshot_dir: str | Path, manifest_path: str | Path | None = None) -> dict:
    snapshot_dir = Path(snapshot_dir).resolve()
    trades = run_backtest(parquet_dir=str(snapshot_dir), quiet=True)
    attrs = dict(trades.attrs)
    nav = attrs.pop("nav")
    nav_0050 = attrs.pop("nav_0050")
    manifest_hash = None
    manifest_content_hash = None
    if manifest_path:
        manifest_path = Path(manifest_path).resolve()
        manifest_hash = sha256_file(manifest_path)
        with manifest_path.open(encoding="utf-8") as handle:
            manifest_content_hash = json.load(handle).get("content_sha256")
    report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "snapshot_dir": str(snapshot_dir),
        "snapshot_manifest_sha256": manifest_hash,
        "snapshot_content_sha256": manifest_content_hash,
        "strategy_config_sha256": canonical_json_sha256(_jsonable(STRATEGY)),
        "trades_sha256": _frame_sha256(trades),
        "nav_sha256": _nav_sha256(nav),
        "nav_0050_sha256": _nav_sha256(nav_0050) if nav_0050 else None,
        "trades": len(trades),
        "metrics": attrs,
        "yearly_returns": _yearly(nav),
        "yearly_returns_0050": _yearly(nav_0050) if nav_0050 else {},
    }
    return _jsonable(report)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", default="data/research")
    parser.add_argument("--manifest")
    parser.add_argument("--output", default="reports/swing_backtest_verified.json")
    args = parser.parse_args()
    report = build_report(args.snapshot, manifest_path=args.manifest)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(_jsonable(report), fh, ensure_ascii=False, indent=2, allow_nan=False)
        fh.write("\n")
    print(json.dumps(_jsonable(report), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
