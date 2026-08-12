"""Cross-check D4 magnitudes against official TWSE Fact Book aggregates."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_SOURCE = "https://wwwc.twse.com.tw/downloads/zh/about/company/factbook/2010/1.01.htm"
# Fact Book unit: million shares and NTD million.  Expanded below to base units.
OFFICIAL = {
    2005: (538_995_000_000, 15_633_858_000_000),
    2006: (549_493_000_000, 19_376_975_000_000),
    2007: (555_864_000_000, 21_527_298_000_000),
    2008: (569_040_000_000, 11_706_527_000_000),
    2009: (577_290_000_000, 21_033_640_000_000),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--snapshot", type=Path,
        default=ROOT / "data" / "research_versions" /
        "twse_market_structure_2005_2014_staging_v1",
    )
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "reports" / "twse_market_structure_factbook_crosscheck_2005_2009.json",
    )
    args = parser.parse_args()
    data = pd.read_parquet(args.snapshot / "market_structure_monthly.parquet")
    data["snapshot_date"] = pd.to_datetime(data["snapshot_date"])
    rows = []
    for year, (official_shares, official_market_cap) in OFFICIAL.items():
        annual = data[data["snapshot_date"].dt.year.eq(year)]
        year_end = annual[annual["snapshot_date"].eq(annual["snapshot_date"].max())]
        observed_shares = int(year_end["issued_shares"].sum())
        observed_market_cap = float(year_end["market_cap_twd"].sum(min_count=1))
        rows.append({
            "year": year,
            "snapshot_date": year_end["snapshot_date"].iloc[0].date().isoformat(),
            "official_listed_shares": official_shares,
            "observed_issued_shares": observed_shares,
            "issued_vs_listed_shares_pct_difference":
                (observed_shares / official_shares - 1) * 100,
            "official_market_cap_twd": official_market_cap,
            "observed_market_cap_twd": observed_market_cap,
            "market_cap_pct_difference":
                (observed_market_cap / official_market_cap - 1) * 100,
        })
    report = {
        "official_source": OFFICIAL_SOURCE,
        "official_share_semantics": "listed_shares",
        "d4_share_semantics": "issued_shares",
        "share_counts_are_not_expected_to_match_exactly": True,
        "market_cap_unit_multiple_check_threshold_pct": 1.0,
        "maximum_absolute_market_cap_pct_difference": max(
            abs(row["market_cap_pct_difference"]) for row in rows
        ),
        "passed_market_cap_unit_multiple_check": all(
            abs(row["market_cap_pct_difference"]) <= 1.0 for row in rows
        ),
        "years": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
