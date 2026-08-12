"""Build an immutable TWSE altered-trading component from verified daily raw files.

補上 `SPEC_DATA_FOUNDATION_AND_MOMENTUM.md` §7.1.6 的另一半：處置由
`twse_disposition_punish_2005_2014` 提供，本元件提供**變更交易方法／
全額交割**。兩者合併後，MOM-1 的不可交易排除才算涵蓋 TWSE。

與處置元件一致的性質：
- 只讀已凍結的 raw cache，先驗 transfer manifest 才開始正規化。
- 逐檔記錄 lineage（raw 路徑與 SHA-256），可回推每一列的來源。
- 原子發布、不覆寫既有 snapshot。
- 要求乾淨的 Git worktree，避免把未提交的程式碼狀態混進不可變元件。

品質閘門的重點在「日曆完整性」：官方每個交易日都會發佈完整名單，因此
**缺一天 raw 就等於當天的排除規則失效**。與其讓那天看起來乾淨，不如讓
`promotion_ready` 直接為 false。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.snapshot_manifest import (  # noqa: E402
    build_manifest,
    build_quality_report,
    sha256_file,
    write_json,
)
from research.twse_altered_trading import (  # noqa: E402
    normalize_payload,
    parse_title_date,
    read_raw,
)
from scripts.backfill_twse_altered_trading import raw_path, trading_days  # noqa: E402
from scripts.build_data_transfer_manifest import verify_manifest  # noqa: E402

REQUIRED = {
    "altered_trading_observations.parquet",
    "twse_altered_trading_source_status.parquet",
}
OFFICIAL_ENDPOINT = "https://www.twse.com.tw/exchangeReport/TWT85U"


def build_snapshot_data(raw_root: Path, days: list[pd.Timestamp],
                        data_base: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Normalize every trading-day archive with per-file lineage."""
    observations: list[pd.DataFrame] = []
    status: list[dict] = []
    for day in days:
        path = raw_path(raw_root, day)
        if not path.exists():
            raise FileNotFoundError(
                f"缺少 {day:%Y-%m-%d} 的官方回應；交易日未全覆蓋不得建立元件: {path}"
            )
        payload = read_raw(path)
        digest = sha256_file(path)
        frame = normalize_payload(payload, raw_sha256=digest, fallback_date=day)

        title_date = parse_title_date(payload.get("title") or "")
        if title_date is not None and title_date != day:
            raise ValueError(
                f"{day:%Y-%m-%d} 的官方標題日期為 {title_date:%Y-%m-%d}；"
                "不得把不同日期的名單當成該日資料"
            )
        observations.append(frame)
        status.append({
            "snapshot_date": day,
            "official_stat": payload.get("stat"),
            "report_title": (payload.get("title") or "").strip(),
            "source_schema": frame["source_schema"].iloc[0] if len(frame) else None,
            "raw_rows": len(payload.get("data") or []),
            "normalized_rows": int(len(frame)),
            "raw_path": path.resolve().relative_to(data_base.resolve()).as_posix(),
            "raw_sha256": digest,
        })

    columns = observations[0].columns if observations else None
    merged = (pd.concat(observations, ignore_index=True) if observations
              else pd.DataFrame(columns=columns))
    if not merged.empty:
        merged = merged.sort_values(["snapshot_date", "stock_id"]).reset_index(drop=True)
    return merged, pd.DataFrame(status)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2005-01-01")
    parser.add_argument("--end", default="2014-12-31")
    parser.add_argument("--snapshot-id", default="twse_altered_trading_2005_2014_v1")
    parser.add_argument("--raw-root", type=Path,
                        default=ROOT / "data/raw/twse/altered_trading")
    parser.add_argument("--calendar", type=Path,
                        default=ROOT / "data/research_versions/twse_prices_2005_2014_v1/prices.parquet")
    parser.add_argument("--output-root", type=Path, default=ROOT / "data/research_versions")
    parser.add_argument("--raw-manifest", type=Path, required=True)
    parser.add_argument("--data-base", type=Path, default=ROOT)
    args = parser.parse_args()

    raw_manifest = json.loads(args.raw_manifest.read_text(encoding="utf-8"))
    raw_verification = verify_manifest(args.data_base, raw_manifest)
    if not raw_verification["passed"]:
        raise ValueError(f"raw transfer manifest verification failed: {raw_verification}")

    days = trading_days(args.calendar, args.start, args.end)
    observations, status = build_snapshot_data(args.raw_root, days, args.data_base)

    target = args.output_root / args.snapshot_id
    if target.exists():
        raise FileExistsError(f"snapshot already exists and will not be overwritten: {target}")
    temporary = args.output_root / f".{args.snapshot_id}.tmp-{uuid.uuid4().hex}"
    temporary.mkdir(parents=True)
    try:
        observations.to_parquet(
            temporary / "altered_trading_observations.parquet", index=False)
        status.to_parquet(
            temporary / "twse_altered_trading_source_status.parquet", index=False)

        manifest = build_manifest(temporary, ROOT)
        if not manifest["git"].get("commit"):
            raise RuntimeError(
                "release snapshot requires a readable Git commit; build from a trusted worktree")
        if manifest["git"].get("dirty"):
            raise RuntimeError("release snapshot requires a clean Git worktree")
        manifest.update({
            "snapshot_id": args.snapshot_id,
            "snapshot_dir": target.resolve().relative_to(
                args.data_base.resolve()).as_posix(),
            "source": {
                "provider": "TWSE",
                "report": "TWT85U",
                "official_endpoint": OFFICIAL_ENDPOINT,
                "start": args.start,
                "end": args.end,
                "raw_manifest": args.raw_manifest.resolve().relative_to(
                    args.data_base.resolve()).as_posix(),
                "raw_manifest_sha256": sha256_file(args.raw_manifest),
                "raw_collection_sha256": raw_manifest["collection_sha256"],
            },
        })

        base_quality = build_quality_report(temporary, manifest, required_files=REQUIRED)

        expected_days = len(days)
        observed_days = int(status["snapshot_date"].nunique())
        all_official_ok = bool(status["official_stat"].eq("OK").all())
        duplicate_keys = int(observations.duplicated(
            ["snapshot_date", "stock_id"]).sum()) if not observations.empty else 0

        schema_counts = status["source_schema"].value_counts(dropna=True).to_dict()
        early = observations[observations["source_schema"] == "code_name_only"]
        late = observations[observations["source_schema"] == "with_periodic_call_auction"]
        # 早期不揭露分盤集合競價；若這裡不是全部缺值，代表 parser 誤補了值。
        early_periodic_all_missing = bool(early["periodic_call_auction"].isna().all())
        late_periodic_known = int(late["periodic_call_auction"].notna().sum())

        component_ready = bool(
            base_quality["structural_passed"]
            and raw_verification["passed"]
            and observed_days == expected_days
            and all_official_ok
            and duplicate_keys == 0
            and early_periodic_all_missing
            and not observations.empty
        )

        quality = {
            **base_quality,
            "scope": f"TWSE_altered_trading_{args.start}_{args.end}",
            "observation_rows": int(len(observations)),
            "stock_ids": int(observations["stock_id"].nunique()) if not observations.empty else 0,
            "expected_trading_days": expected_days,
            "observed_trading_days": observed_days,
            "trading_day_coverage_complete": observed_days == expected_days,
            "all_official_stat_ok": all_official_ok,
            "duplicate_primary_keys": duplicate_keys,
            "days_without_any_altered_security": int((status["normalized_rows"] == 0).sum()),
            "source_schema_days": {str(k): int(v) for k, v in schema_counts.items()},
            "early_schema_rows": int(len(early)),
            "late_schema_rows": int(len(late)),
            "early_periodic_call_auction_all_missing": early_periodic_all_missing,
            "late_periodic_call_auction_known_rows": late_periodic_known,
            "raw_manifest_verified": raw_verification["passed"],
            "raw_collection_sha256": raw_manifest["collection_sha256"],
            "twse_altered_trading_component_ready": component_ready,
            "promotion_ready": component_ready,
            "units": {
                "altered_trading": "boolean_official_listed_that_session",
                "periodic_call_auction": "boolean_or_missing_not_false_before_schema_change",
            },
            "known_gaps": [
                "early TWSE reports disclose no periodic-call-auction flag; those rows are missing, never False",
                "TWSE suspension (停止交易) has no separate official flag in this component",
                "TPEx altered-trading history is supplied by the separate D5 snapshot",
            ],
        }
        write_json(temporary / "manifest.json", manifest)
        write_json(temporary / "quality_report.json", quality)
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(target)
    print(manifest["content_sha256"])
    print(json.dumps({
        "observation_rows": quality["observation_rows"],
        "stock_ids": quality["stock_ids"],
        "trading_days": f"{observed_days}/{expected_days}",
        "component_ready": component_ready,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
