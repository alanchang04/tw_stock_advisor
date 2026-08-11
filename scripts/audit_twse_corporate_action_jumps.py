"""Reconcile large raw-price jumps against official TWSE corporate actions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def build_jump_audit(prices: pd.DataFrame, events: pd.DataFrame,
                     threshold: float = 0.20,
                     security_master: pd.DataFrame | None = None,
                     ) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    prices = prices[["stock_id", "trade_date", "open", "close"]].copy()
    prices["stock_id"] = prices["stock_id"].astype(str)
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    prices = prices.sort_values(["stock_id", "trade_date"])
    prices["previous_trade_date"] = prices.groupby("stock_id")["trade_date"].shift()
    prices["previous_close"] = prices.groupby("stock_id")["close"].shift()
    prices["calendar_gap_days"] = (
        prices["trade_date"] - prices["previous_trade_date"]
    ).dt.days
    prices["close_return"] = prices["close"] / prices["previous_close"] - 1.0
    prices["open_gap"] = prices["open"] / prices["previous_close"] - 1.0
    prices["observation_number"] = prices.groupby("stock_id").cumcount() + 1

    events = events.copy()
    events["stock_id"] = events["stock_id"].astype(str)
    events["event_date"] = pd.to_datetime(events["event_date"])

    price_groups = {
        stock_id: group.reset_index(drop=True)
        for stock_id, group in prices.groupby("stock_id", sort=False)
    }
    mapped_records = []
    context_columns = [
        "trade_date", "previous_trade_date", "previous_close", "open", "close",
        "calendar_gap_days", "open_gap", "close_return",
    ]
    for event in events.to_dict("records"):
        context = price_groups.get(event["stock_id"])
        selected = None
        if context is not None:
            position = context["trade_date"].searchsorted(event["event_date"], side="left")
            if position < len(context):
                candidate = context.iloc[position]
                if (candidate["trade_date"] - event["event_date"]).days <= 7:
                    selected = candidate
        mapped = dict(event)
        for column in context_columns:
            mapped[column] = selected[column] if selected is not None else pd.NaT if "date" in column else None
        mapped_records.append(mapped)
    if mapped_records:
        event_checks = pd.DataFrame(mapped_records)
    else:
        event_checks = events.copy()
        for column in context_columns:
            event_checks[column] = pd.Series(
                dtype="datetime64[ns]" if "date" in column else "float64"
            )
    event_checks["effective_date_lag_days"] = (
        event_checks["trade_date"] - event_checks["event_date"]
    ).dt.days
    event_checks["has_price_on_effective_date"] = event_checks["trade_date"].notna()
    event_checks["official_pre_close_matches"] = (
        (event_checks["previous_close"] - event_checks["pre_event_close"]).abs() <= 0.011
    ).where(event_checks["previous_close"].notna())

    matched_events = event_checks[event_checks["has_price_on_effective_date"]].copy()
    grouped_events = matched_events.groupby(["stock_id", "trade_date"], as_index=False).agg(
        official_event_date=("event_date", "min"),
        event_kinds=("event_kind", lambda values: "|".join(sorted(set(values)))),
        source_reports=("source_report", lambda values: "|".join(sorted(set(values)))),
        official_pre_close=("pre_event_close", "first"),
        official_reference_price=("reference_price", "first"),
        official_adjustment_factor=("adjustment_factor", "first"),
    )

    jumps = prices[prices["close_return"].abs().gt(threshold)].copy()
    jumps = jumps.merge(
        grouped_events,
        left_on=["stock_id", "trade_date"],
        right_on=["stock_id", "trade_date"],
        how="left",
    )
    jumps["classification"] = "unexplained_short_gap"
    jumps.loc[jumps["event_kinds"].notna(), "classification"] = "matched_corporate_action"
    if security_master is not None:
        master = security_master[["stock_id", "asset_type", "listing_date"]].copy()
        master["stock_id"] = master["stock_id"].astype(str)
        master["listing_date"] = pd.to_datetime(master["listing_date"])
        jumps = jumps.merge(master, on="stock_id", how="left")
        no_event = jumps["event_kinds"].isna()
        jumps.loc[
            no_event & jumps["asset_type"].notna()
            & ~jumps["asset_type"].eq("common_stock"),
            "classification",
        ] = "excluded_non_common_security"
        jumps.loc[
            no_event & jumps["asset_type"].eq("common_stock")
            & jumps["listing_date"].notna()
            & jumps["trade_date"].ge(jumps["listing_date"])
            & jumps["observation_number"].le(5),
            "classification",
        ] = "new_listing_price_discovery"
    jumps.loc[
        jumps["classification"].eq("unexplained_short_gap")
        & jumps["calendar_gap_days"].gt(7),
        "classification",
    ] = "long_observation_gap_manual_review"
    jumps["official_pre_close_matches"] = (
        (jumps["previous_close"] - jumps["official_pre_close"]).abs() <= 0.011
    ).where(jumps["event_kinds"].notna())

    classifications = jumps["classification"].value_counts().sort_index().to_dict()
    matched = jumps[jumps["classification"].eq("matched_corporate_action")]
    report = {
        "threshold_absolute_return": threshold,
        "price_rows": len(prices),
        "event_rows": len(events),
        "large_jump_rows": len(jumps),
        "jump_classifications": classifications,
        "matched_jump_pre_close_checks": int(matched["official_pre_close_matches"].notna().sum()),
        "matched_jump_pre_close_mismatches": int(matched["official_pre_close_matches"].eq(False).sum()),
        "events_with_effective_trade_date": int(event_checks["has_price_on_effective_date"].sum()),
        "events_without_effective_trade_date": int((~event_checks["has_price_on_effective_date"]).sum()),
        "events_shifted_to_later_trade_date": int(event_checks["effective_date_lag_days"].gt(0).sum()),
        "event_pre_close_checks": int(event_checks["official_pre_close_matches"].notna().sum()),
        "event_pre_close_mismatches": int(event_checks["official_pre_close_matches"].eq(False).sum()),
        "passed_no_unexplained_short_gap": classifications.get("unexplained_short_gap", 0) == 0,
    }
    return report, jumps, event_checks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--price-snapshot",
        type=Path,
        default=ROOT / "data" / "research_versions" / "twse_prices_2005_2014_v1",
    )
    parser.add_argument(
        "--action-snapshot",
        type=Path,
        default=(ROOT / "data" / "research_versions" /
                 "twse_corporate_actions_2005_2014_staging_v1"),
    )
    parser.add_argument(
        "--security-master-snapshot",
        type=Path,
        default=(ROOT / "data" / "research_versions" /
                 "twse_security_master_2005_2014_staging_v1"),
    )
    parser.add_argument("--threshold", type=float, default=0.20)
    parser.add_argument(
        "--output-prefix",
        type=Path,
        default=ROOT / "reports" / "twse_corporate_action_jump_audit_2005_2014",
    )
    args = parser.parse_args()

    prices = pd.read_parquet(args.price_snapshot / "prices.parquet")
    events = pd.read_parquet(args.action_snapshot / "corporate_actions.parquet")
    security_master = pd.read_parquet(
        args.security_master_snapshot / "security_master_staging.parquet"
    )
    report, jumps, event_checks = build_jump_audit(
        prices, events, args.threshold, security_master
    )
    for frame in (jumps, event_checks):
        if "raw_path" in frame:
            frame["raw_path"] = frame["raw_path"].map(
                lambda value: Path(value).resolve().relative_to(ROOT.resolve()).as_posix()
                if pd.notna(value) and Path(value).is_absolute()
                and Path(value).resolve().is_relative_to(ROOT.resolve())
                else value
            )

    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = args.output_prefix.with_suffix(".json")
    jumps_path = args.output_prefix.with_name(args.output_prefix.name + "_jumps.csv")
    events_path = args.output_prefix.with_name(args.output_prefix.name + "_events.csv")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    jumps.to_csv(jumps_path, index=False, encoding="utf-8-sig")
    event_checks.to_csv(events_path, index=False, encoding="utf-8-sig")
    print(json.dumps({
        **report,
        "report": str(json_path.resolve()),
        "jumps": str(jumps_path.resolve()),
        "events": str(events_path.resolve()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
