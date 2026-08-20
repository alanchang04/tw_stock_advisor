"""Freeze the official TPEx D5 staging data and run promotion-boundary audits."""
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

from research.snapshot_manifest import build_manifest, build_quality_report, write_json


REQUIRED = {
    "prices.parquet",
    "dividend_events.parquet",
    "stocks.parquet",
    "delisted_stocks.parquet",
    "stock_universe_history.parquet",
    "notice_events.parquet",
    "disposition_events.parquet",
    "trading_restrictions.parquet",
    "tpex_source_status.parquet",
    "institutional_availability.parquet",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source", type=Path,
        default=ROOT / "data" / "research" / "tpex_history_2008_2014",
    )
    parser.add_argument("--snapshot-id", default="tpex_history_2008_2014_staging_v1")
    parser.add_argument(
        "--output-root", type=Path, default=ROOT / "data" / "research_versions",
    )
    args = parser.parse_args()
    target = args.output_root / args.snapshot_id
    if target.exists():
        raise FileExistsError(f"snapshot already exists and will not be overwritten: {target}")
    temporary = args.output_root / f".{args.snapshot_id}.tmp-{uuid.uuid4().hex}"
    temporary.mkdir(parents=True)
    try:
        for name in REQUIRED:
            shutil.copy2(args.source / name, temporary / name)

        prices = pd.read_parquet(temporary / "prices.parquet")
        universe = pd.read_parquet(temporary / "stock_universe_history.parquet")
        delisted = pd.read_parquet(temporary / "delisted_stocks.parquet")
        actions = pd.read_parquet(temporary / "dividend_events.parquet")
        attention = pd.read_parquet(temporary / "notice_events.parquet")
        disposal = pd.read_parquet(temporary / "disposition_events.parquet")
        restrictions = pd.read_parquet(temporary / "trading_restrictions.parquet")
        status = pd.read_parquet(temporary / "tpex_source_status.parquet")
        probes = pd.read_parquet(temporary / "institutional_availability.parquet")
        source_quality = json.loads(
            (args.source / "quality_report.json").read_text(encoding="utf-8")
        )

        manifest = build_manifest(temporary, ROOT)
        manifest.update({
            "snapshot_id": args.snapshot_id,
            "snapshot_dir": target.resolve().relative_to(ROOT.resolve()).as_posix(),
            "source": {
                "provider": "TPEx",
                "official_base": "https://www.tpex.org.tw/www/zh-tw",
                "legacy_official_base": "https://hist.tpex.org.tw/Hist",
                "source_queries": int(len(status)),
                "calendar_snapshot": "twse_prices_2005_2014_v1",
            },
        })
        quality = build_quality_report(temporary, manifest, required_files=REQUIRED)

        price_dates = set(prices["trade_date"].astype(str))
        universe_dates = set(universe["snapshot_date"].astype(str))
        daily_status = status[status["source_type"].eq("daily_quotes")]
        restriction_status = status[
            status["source_type"].isin(["trading_restrictions", "trading_restrictions_legacy"])
        ]
        source_date_coverage = (
            set(daily_status["query_key"].astype(str)) == price_dates
            and set(restriction_status["query_key"].astype(str)) == price_dates
        )
        universe_exact = bool(
            universe_dates == price_dates
            and len(universe) == len(prices)
            and not universe.duplicated(["snapshot_date", "stock_id"]).any()
        )

        for frame, column in ((prices, "volume"), (prices, "turnover"), (prices, "issued_shares")):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        nonintegral_volume = int(
            (prices["volume"].notna() & prices["volume"].ne(prices["volume"].round())).sum()
        )
        nonintegral_shares = int(
            (prices["issued_shares"].notna() & prices["issued_shares"].ne(prices["issued_shares"].round())).sum()
        )
        valid_vwap = (
            prices["volume"].gt(0)
            & prices["turnover"].notna()
            & prices[["low", "high", "close"]].notna().all(axis=1)
        )
        implied = prices.loc[valid_vwap, "turnover"] / prices.loc[valid_vwap, "volume"]
        low = prices.loc[valid_vwap, "low"]
        high = prices.loc[valid_vwap, "high"]
        implied_outside_ohlc = int(((implied < low * 0.999) | (implied > high * 1.001)).sum())
        implied_to_close = implied / prices.loc[valid_vwap, "close"]
        implied_to_close_median = float(implied_to_close.median())
        implied_to_close_min = float(implied_to_close.min())
        implied_to_close_max = float(implied_to_close.max())
        share_volume_unit_sanity = bool(
            0.95 <= implied_to_close_median <= 1.05
            and implied_to_close_min > 0.1
            and implied_to_close_max < 10
        )

        observed = prices[["stock_id", "trade_date"]].copy()
        observed["trade_date"] = pd.to_datetime(observed["trade_date"])
        terminal = delisted[["stock_id", "delisting_date"]].copy()
        terminal["delisting_date"] = pd.to_datetime(terminal["delisting_date"])
        after_delisting = observed.merge(terminal, on="stock_id", how="inner")
        after_delisting_rows = int(
            after_delisting["trade_date"].gt(after_delisting["delisting_date"]).sum()
        )

        equation = (
            pd.to_numeric(actions["pre_close"], errors="coerce")
            - pd.to_numeric(actions["ref_price"], errors="coerce")
            - pd.to_numeric(actions["stock_dividend_value"], errors="coerce").fillna(0)
            - pd.to_numeric(actions["cash_dividend"], errors="coerce").fillna(0)
        )
        action_complete = actions[["pre_close", "ref_price"]].notna().all(axis=1)
        max_action_equation_error = float(equation[action_complete].abs().max() or 0)
        action_equation_over_one_cent = int(
            equation[action_complete].abs().gt(0.011).sum()
        )

        event_ids = set(attention["stock_id"].astype(str)) | set(disposal["stock_id"].astype(str))
        known_ids = set(prices["stock_id"].astype(str)) | set(delisted["stock_id"].astype(str))
        event_ids_without_standard_quotes = event_ids - known_ids
        restriction_ids = set(restrictions["stock_id"].astype(str))
        unexplained_event_ids = sorted(event_ids_without_standard_quotes - restriction_ids)
        restricted_nonstandard_event_ids = sorted(
            event_ids_without_standard_quotes & restriction_ids
        )
        legacy_partial = restrictions["source_detail"].str.contains("legacy", na=False)
        probes_missing_honest = bool(
            probes["availability"].eq("missing").all()
            and probes["missing_semantics"].eq("unknown_not_zero").all()
        )

        structural = bool(
            quality.get("structural_passed")
            and source_date_coverage
            and universe_exact
            and nonintegral_volume == 0
            and nonintegral_shares == 0
            and prices["volume"].dropna().ge(0).all()
            and prices["turnover"].dropna().ge(0).all()
            and prices["issued_shares"].dropna().gt(0).all()
            and share_volume_unit_sanity
            and after_delisting_rows == 0
            and not unexplained_event_ids
            and probes_missing_honest
        )
        blockers = [
            "legacy TPEx restriction files before 2008-04-09 expose altered-trading membership only; other restriction flags remain unknown",
            "pre-2018 TPEx institutional observations are missing/unknown, never zero-filled",
            "D3 corporate-action execution ledger is not promotion-ready",
        ]
        quality.update({
            "snapshot_id": args.snapshot_id,
            "scope": "D5_TPEX_2008_2014_official_history_staging",
            "structural_passed": structural,
            "d5_staging_complete": structural,
            "all_market_deployment_ready": False,
            "promotion_ready": False,
            "rows": {
                "prices": int(len(prices)), "universe": int(len(universe)),
                "corporate_actions": int(len(actions)), "delisted": int(len(delisted)),
                "attention": int(len(attention)), "disposal": int(len(disposal)),
                "trading_restrictions": int(len(restrictions)),
            },
            "stock_ids": int(prices["stock_id"].astype(str).nunique()),
            "trading_dates": int(len(price_dates)),
            "source_date_coverage_complete": source_date_coverage,
            "daily_universe_exact_from_quote_presence": universe_exact,
            "suspended_or_missing_price_rows": int(prices["price_missing"].sum()),
            "nonintegral_volume_rows": nonintegral_volume,
            "nonintegral_issued_shares_rows": nonintegral_shares,
            "implied_vwap_outside_daily_ohlc_rows": implied_outside_ohlc,
            "turnover_implied_price_to_close_median": implied_to_close_median,
            "turnover_implied_price_to_close_min": implied_to_close_min,
            "turnover_implied_price_to_close_max": implied_to_close_max,
            "share_volume_unit_sanity_passed": share_volume_unit_sanity,
            "observations_after_official_delisting_rows": after_delisting_rows,
            "restricted_nonstandard_attention_or_disposal_stock_ids": (
                restricted_nonstandard_event_ids
            ),
            "unexplained_attention_or_disposal_stock_ids": unexplained_event_ids,
            "corporate_action_max_reference_equation_error_twd": max_action_equation_error,
            "corporate_action_equation_error_over_0_011_rows": action_equation_over_one_cent,
            "legacy_partial_restriction_rows": int(legacy_partial.sum()),
            "institutional_probe_missing_semantics_passed": probes_missing_honest,
            "units": source_quality["units"],
            "blockers": blockers,
        })
        write_json(temporary / "manifest.json", manifest)
        write_json(temporary / "quality_report.json", quality)
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(target)
    print(manifest["content_sha256"])


if __name__ == "__main__":
    main()
