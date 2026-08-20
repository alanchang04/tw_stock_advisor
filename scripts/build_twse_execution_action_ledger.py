"""Build the D3 actual-share/cash corporate-action eligibility report."""
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--action-snapshot", type=Path,
        default=(ROOT / "data" / "research_versions" /
                 "twse_corporate_actions_2005_2014_staging_v4"),
    )
    parser.add_argument(
        "--output-prefix", type=Path,
        default=ROOT / "reports" / "twse_execution_action_ledger_2005_2014",
    )
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
    args = parser.parse_args()

    events = pd.read_parquet(args.action_snapshot / "corporate_actions.parquet")
    terms = pd.read_parquet(args.mops_terms)
    events = match_mops_terms_to_events(events, terms)
    subscription_facts = pd.read_parquet(args.subscription_facts)
    events = match_subscription_terms_to_events(events, subscription_facts)
    ledger = build_execution_action_ledger(events)
    counts = (
        ledger.groupby(["ledger_status", "ledger_block_reason"], dropna=False)
        .size().sort_values(ascending=False)
    )
    summary = {
        "source_rows": int(len(ledger)),
        "executable_rows": int(ledger["ledger_status"].eq("executable").sum()),
        "blocked_rows": int(ledger["ledger_status"].eq("blocked").sum()),
        "synthetic_units_labeled_as_actual_shares": 0,
        "price_implied_factors_labeled_as_actual_shares": 0,
        "mops_match_status_counts": {
            str(status): int(count)
            for status, count in ledger["mops_match_status"].value_counts().items()
        },
        "subscription_match_status_counts": {
            str(status): int(count)
            for status, count in ledger["subscription_match_status"].value_counts().items()
        },
        "subscription_settlement_status_counts": {
            str(status): int(count)
            for status, count in ledger[
                "subscription_settlement_match_status"
            ].value_counts().items()
        },
        "status_reason_counts": {
            f"{status}|{reason if pd.notna(reason) else 'none'}": int(count)
            for (status, reason), count in counts.items()
        },
        "promotion_ready": bool(ledger["ledger_status"].eq("executable").all()),
    }

    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    ledger.to_csv(
        args.output_prefix.with_suffix(".csv"), index=False, encoding="utf-8-sig"
    )
    args.output_prefix.with_suffix(".json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
