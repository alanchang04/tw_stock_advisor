"""Reconcile large raw-price jumps against official TWSE corporate actions."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.corporate_action_ledger import build_execution_action_ledger
from research.mops_dividend_distribution import match_mops_terms_to_events
from research.mops_paid_subscription import match_subscription_terms_to_events


def build_jump_audit(prices: pd.DataFrame, events: pd.DataFrame,
                     threshold: float = 0.20,
                     security_master: pd.DataFrame | None = None,
                     ) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    price_columns = ["stock_id", "trade_date", "open", "close"]
    if "change_pct" in prices.columns:
        price_columns.append("change_pct")
    prices = prices[price_columns].copy()
    if "change_pct" not in prices.columns:
        prices["change_pct"] = pd.NA
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
    # MI_INDEX 的 change_pct 是相對「當日交易所參考價」，不是一律相對上次有成交的
    # 收盤價。停牌後若因減資、重整或其他規則重設參考價，直接用 previous_close 算
    # 報酬會製造假的單日暴漲跌。保留兩個維度，避免在原因尚未查明前誤調整價格：
    #   1. reference_reset_return：參考價相對上次觀察收盤的變化；
    #   2. market_return_from_reference：恢復交易當日真正的市場漲跌。
    denominator = 1.0 + pd.to_numeric(prices["change_pct"], errors="coerce") / 100.0
    valid_reference = denominator.gt(0)
    prices["implied_official_reference_price"] = (
        prices["close"] / denominator
    ).where(valid_reference)
    prices["reference_reset_return"] = (
        prices["implied_official_reference_price"] / prices["previous_close"] - 1.0
    )
    prices["market_return_from_reference"] = (
        prices["close"] / prices["implied_official_reference_price"] - 1.0
    )
    prices["observation_number"] = prices.groupby("stock_id").cumcount() + 1

    events = build_execution_action_ledger(events.copy())
    events["stock_id"] = events["stock_id"].astype(str)
    events["event_date"] = pd.to_datetime(events["event_date"])

    price_groups = {
        stock_id: group.reset_index(drop=True)
        for stock_id, group in prices.groupby("stock_id", sort=False)
    }
    mapped_records = []
    context_columns = [
        "trade_date", "previous_trade_date", "previous_close", "open", "close",
        "change_pct", "calendar_gap_days", "open_gap", "close_return",
        "implied_official_reference_price", "reference_reset_return",
        "market_return_from_reference",
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
        event_ledger_eligible=("total_return_eligible", "all"),
        event_ledger_block_reasons=(
            "ledger_block_reason",
            lambda values: "|".join(sorted({str(value) for value in values if pd.notna(value)})),
        ),
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
    # 這只能證明「交易所重設參考價」，不能證明原因一定是減資。若沒有公司行動來源，
    # 維持 total_return_eligible=False；待減資／合併／分割等官方證據配對後才能升級。
    confirmed_reference_reset = (
        jumps["classification"].eq("long_observation_gap_manual_review")
        & jumps["implied_official_reference_price"].notna()
        & jumps["reference_reset_return"].abs().gt(threshold)
        & jumps["market_return_from_reference"].abs().le(0.10 + 1e-9)
    )
    jumps.loc[
        confirmed_reference_reset,
        "classification",
    ] = "official_reference_reset_unresolved_cause"
    jumps["total_return_eligible"] = (
        jumps["classification"].eq("matched_corporate_action")
        & jumps["event_ledger_eligible"].eq(True)
    )
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
        "official_reference_resets_unresolved_cause": int(
            jumps["classification"].eq(
                "official_reference_reset_unresolved_cause"
            ).sum()
        ),
        "reference_resets_promoted_to_total_return_without_event": int(
            (
                jumps["classification"].eq(
                    "official_reference_reset_unresolved_cause"
                )
                & jumps["total_return_eligible"]
            ).sum()
        ),
        "matched_corporate_action_jumps_not_ledger_executable": int(
            (
                jumps["classification"].eq("matched_corporate_action")
                & ~jumps["total_return_eligible"]
            ).sum()
        ),
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
                 "twse_corporate_actions_2005_2014_staging_v4"),
    )
    parser.add_argument(
        "--security-master-snapshot",
        type=Path,
        default=(ROOT / "data" / "research_versions" /
                 "twse_security_master_2005_2014_staging_v1"),
    )
    parser.add_argument("--threshold", type=float, default=0.20)
    parser.add_argument(
        "--mops-terms", type=Path,
        default=(ROOT / "data" / "research_versions" /
                 "mops_dividend_distributions_2004_2014_v1" /
                 "dividend_terms.parquet"),
    )
    parser.add_argument(
        "--subscription-facts", type=Path,
        default=(ROOT / "data" / "research_versions" /
                 "mops_paid_subscription_announcements_2004_2015_v4" /
                 "subscription_facts.parquet"),
    )
    parser.add_argument(
        "--output-prefix",
        type=Path,
        default=ROOT / "reports" / "twse_corporate_action_jump_audit_2005_2014",
    )
    args = parser.parse_args()

    prices = pd.read_parquet(args.price_snapshot / "prices.parquet")
    events = pd.read_parquet(args.action_snapshot / "corporate_actions.parquet")
    terms = pd.read_parquet(args.mops_terms)
    events = match_mops_terms_to_events(events, terms)
    subscription_facts = pd.read_parquet(args.subscription_facts)
    events = match_subscription_terms_to_events(events, subscription_facts)
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
