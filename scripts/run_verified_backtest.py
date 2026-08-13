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


def write_curve_artifact(output_dir: Path, trades: pd.DataFrame, nav: dict,
                         nav_0050: dict, report: dict) -> dict:
    """把 NAV 序列與交易明細另存為小型 artifact，供 Streamlit 畫圖。

    為什麼要有這個：`reports/swing_backtest_verified_*.json` 只保存 NAV 的
    **雜湊**（3KB 的驗證檔），沒有序列本身，因此畫不出權益曲線。原始價量有
    480 萬列、數 GB，但 NAV 只有每日兩個數字——把成品另存可以讓前端不必接觸
    任何原始研究資料。

    artifact 內嵌 snapshot 與 NAV 的 SHA-256，因此圖表可以標示自己是從哪一份
    快照算出來的，且任何人都能驗證它沒有被手動編輯過。

    **只適用現行波段策略。** MOM-1／REV-1 的 NAV 一旦產出即等同開封 holdout，
    是 release `usage_policy` 明文禁止的行為。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    curve = pd.DataFrame({
        "trade_date": pd.to_datetime(sorted(nav)),
        "strategy_nav": [float(nav[k]) for k in sorted(nav)],
    })
    if nav_0050:
        bench = pd.Series({pd.Timestamp(k): float(v) for k, v in nav_0050.items()})
        curve["benchmark_nav"] = curve["trade_date"].map(bench)
    curve.to_parquet(output_dir / "nav_curve.parquet", index=False)
    # trades.attrs 仍帶著 metrics（含 date 物件），pandas 會試圖把它塞進 parquet
    # metadata 而失敗；這裡只輸出資料本身，metrics 由 provenance.json 承載。
    trades_out = trades.copy()
    trades_out.attrs = {}
    trades_out.to_parquet(output_dir / "trades.parquet", index=False)

    provenance = {
        "generated_at": report["generated_at"],
        "snapshot_content_sha256": report["snapshot_content_sha256"],
        "strategy_config_sha256": report["strategy_config_sha256"],
        "nav_sha256": report["nav_sha256"],
        "nav_0050_sha256": report["nav_0050_sha256"],
        "trades_sha256": report["trades_sha256"],
        "trading_days": int(len(curve)),
        "trades": int(len(trades)),
        "scope": "current_swing_strategy_only_never_MOM1_or_REV1",
    }
    with (output_dir / "provenance.json").open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(provenance, fh, ensure_ascii=False, indent=2, allow_nan=False)
        fh.write("\n")
    return provenance


def build_report(snapshot_dir: str | Path, manifest_path: str | Path | None = None,
                 curve_output: str | Path | None = None) -> dict:
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
    report = _jsonable(report)
    if curve_output:
        write_curve_artifact(Path(curve_output), trades, nav, nav_0050 or {}, report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", default="data/research")
    parser.add_argument("--manifest")
    parser.add_argument("--output", default="reports/swing_backtest_verified.json")
    parser.add_argument("--curve-output",
                        help="另存 NAV 序列與交易明細供前端畫圖；不影響既有報告內容")
    args = parser.parse_args()
    report = build_report(args.snapshot, manifest_path=args.manifest,
                          curve_output=args.curve_output)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(_jsonable(report), fh, ensure_ascii=False, indent=2, allow_nan=False)
        fh.write("\n")
    print(json.dumps(_jsonable(report), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
