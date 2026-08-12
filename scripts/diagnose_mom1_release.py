"""Run deterministic MOM-1 PIT signal-count diagnostics on a named release.

This command never calculates returns or performance.  It is safe before the
backward-holdout gate opens and is intended to produce byte-identical output on
the Data Authority and deployment machines.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.momentum import (  # noqa: E402
    ENTRY_TOP_FRAC,
    EXIT_BOTTOM_FRAC,
    LIQUIDITY_TOP_FRAC,
    LIQUIDITY_WINDOW,
    MIN_HISTORY_SESSIONS,
    MIN_PRICE,
    compute_mom_6_1,
    eligible_universe,
    month_end_sessions,
    next_session,
    pit_common_stock_mask,
    pit_common_stock_mask_from_universe_history,
    select_holdings,
)
from research.momentum_release import load_momentum_release, sha256_file  # noqa: E402


STRATEGY_PATHS = (
    "research/momentum.py",
    "research/momentum_release.py",
    "scripts/diagnose_mom1_release.py",
    "docs/SPEC_DATA_FOUNDATION_AND_MOMENTUM.md",
)


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _industry_snapshot(frame: pd.DataFrame, as_of: pd.Timestamp) -> tuple[str | None, pd.DataFrame]:
    past = frame[frame["snapshot_date"] <= as_of]
    if past.empty:
        return None, past
    snapshot_date = past["snapshot_date"].max()
    return snapshot_date.date().isoformat(), past[past["snapshot_date"] == snapshot_date]


def run(release_id: str) -> dict:
    strategy_dirty = _git("status", "--porcelain", "--", *STRATEGY_PATHS)
    if strategy_dirty:
        raise RuntimeError(
            "strategy/release adapter 必須先 commit 才能產生可復現診斷:\n" + strategy_dirty
        )

    strategy_commit = _git("log", "-1", "--format=%H", "--", *STRATEGY_PATHS)

    inputs = load_momentum_release(release_id, root=ROOT)
    signal = compute_mom_6_1(inputs.adjusted_close)
    decisions = month_end_sessions(inputs.trading_days)
    market_structure = inputs.market_structure.copy()
    market_structure["snapshot_date"] = pd.to_datetime(market_structure["snapshot_date"])
    market_structure["stock_id"] = market_structure["stock_id"].astype(str)

    monthly: list[dict] = []
    previous_holdings: list[str] = []
    for decision in decisions:
        master_mask = pit_common_stock_mask(
            inputs.pit_master, decision, inputs.adjusted_close.columns
        )
        history_mask = pit_common_stock_mask_from_universe_history(
            inputs.universe_history, decision, inputs.adjusted_close.columns
        )
        mask_mismatch = int((master_mask != history_mask).sum())

        baseline = eligible_universe(
            as_of=decision,
            adjusted_close=inputs.adjusted_close,
            raw_close=inputs.raw_close,
            turnover=inputs.turnover,
            signal=signal,
            pit_mask=master_mask,
            allow_missing_restrictions=True,
        )
        disposition_eligible = eligible_universe(
            as_of=decision,
            adjusted_close=inputs.adjusted_close,
            raw_close=inputs.raw_close,
            turnover=inputs.turnover,
            signal=signal,
            pit_mask=master_mask,
            restricted=inputs.restricted,
        )
        values = signal.loc[decision, disposition_eligible]
        holdings = select_holdings(values, previous_holdings, max_positions=10)

        industry_date, industry = _industry_snapshot(market_structure, decision)
        industry_lookup = industry.set_index("stock_id")["industry_code_asof"] if not industry.empty else pd.Series(dtype=object)
        selected_industries = industry_lookup.reindex(holdings)
        industry_missing = int(selected_industries.isna().sum())

        execution = next_session(inputs.trading_days, decision)
        monthly.append({
            "decision_date": decision.date().isoformat(),
            "execution_date": None if execution is None else execution.date().isoformat(),
            "pit_common_count": int(master_mask.sum()),
            "monthly_history_mask_mismatch_count": mask_mismatch,
            "eligible_before_disposition_count": len(baseline),
            "eligible_after_disposition_count": len(disposition_eligible),
            "disposition_excluded_count": len(set(baseline) - set(disposition_eligible)),
            "entry_slot_count": int(len(disposition_eligible) * ENTRY_TOP_FRAC + 1e-9),
            "selected_count": len(holdings),
            "selected_stock_ids": holdings,
            "industry_snapshot_date": industry_date,
            "selected_missing_pit_industry_count": industry_missing,
        })
        previous_holdings = holdings

    active = [row for row in monthly if row["eligible_after_disposition_count"] > 0]
    spec_path = ROOT / "docs/SPEC_DATA_FOUNDATION_AND_MOMENTUM.md"
    result = {
        "schema_version": 1,
        "diagnostic": "MOM1 named-release PIT signal counts",
        "performance_inspected": False,
        "data_release_id": inputs.release_id,
        "release_descriptor": inputs.descriptor_path.relative_to(ROOT).as_posix(),
        "release_descriptor_sha256": inputs.descriptor_sha256,
        "component_content_sha256": inputs.component_content_sha256,
        "verified_input_sha256": inputs.verified_input_sha256,
        # Bind reproducibility to the last commit that changed the strategy contract.
        # Later report/deployment-only commits must not change deterministic output.
        "git_commit": strategy_commit,
        "strategy_paths_clean_at_run": True,
        "strategy_spec_sha256": sha256_file(spec_path),
        "command": (
            ".\\.venv-repro\\Scripts\\python.exe "
            ".\\scripts\\diagnose_mom1_release.py "
            f"--release-id {release_id} --output reports\\mom1_release_diagnostic.json"
        ),
        "rules": {
            "minimum_price_twd": MIN_PRICE,
            "minimum_history_sessions": MIN_HISTORY_SESSIONS,
            "liquidity_window_sessions_complete": LIQUIDITY_WINDOW,
            "liquidity_top_fraction": LIQUIDITY_TOP_FRAC,
            "entry_top_fraction_floor": ENTRY_TOP_FRAC,
            "retention_top_fraction_floor": EXIT_BOTTOM_FRAC,
            "maximum_positions": 10,
            "existing_holdings_have_buffer_priority": True,
        },
        "scope": {
            "adjustment": inputs.adjustment_scope,
            "restrictions": inputs.restriction_scope,
            "authoritative_pit_path": "interval master effective_from <= d < delisting_date",
            "monthly_history_path": "audit comparison only; never reads a future snapshot",
        },
        "coverage": {
            "price_start": inputs.raw_close.index.min().date().isoformat(),
            "price_end": inputs.raw_close.index.max().date().isoformat(),
            "trading_sessions": len(inputs.raw_close.index),
            "price_stock_ids": len(inputs.raw_close.columns),
            "decision_months": len(monthly),
            "months_with_nonempty_eligible_universe": len(active),
            "disposition_events": len(inputs.disposition_events),
            "disposition_restricted_stock_sessions": int(
                inputs.disposition_restricted.to_numpy().sum()
            ),
            "altered_trading_observations": len(inputs.altered_trading_observations),
            "altered_trading_restricted_stock_sessions": int(
                inputs.altered_trading_restricted.to_numpy().sum()
            ),
            "combined_restricted_stock_sessions": int(inputs.restricted.to_numpy().sum()),
        },
        "summary": {
            "first_nonempty_decision": active[0]["decision_date"] if active else None,
            "last_nonempty_decision": active[-1]["decision_date"] if active else None,
            "median_eligible_before_disposition": (
                float(pd.Series([r["eligible_before_disposition_count"] for r in active]).median())
                if active else None
            ),
            "median_eligible_after_disposition": (
                float(pd.Series([r["eligible_after_disposition_count"] for r in active]).median())
                if active else None
            ),
            "months_with_disposition_exclusion": sum(
                row["disposition_excluded_count"] > 0 for row in monthly
            ),
            "total_monthly_disposition_exclusions": sum(
                row["disposition_excluded_count"] for row in monthly
            ),
            "months_with_master_history_mask_mismatch": sum(
                row["monthly_history_mask_mismatch_count"] > 0 for row in monthly
            ),
            "maximum_master_history_mask_mismatch": max(
                (row["monthly_history_mask_mismatch_count"] for row in monthly), default=0
            ),
            "months_with_selected_missing_pit_industry": sum(
                row["selected_missing_pit_industry_count"] > 0 for row in monthly
            ),
        },
        "known_blockers": [
            "D3 actual-share execution ledger and unresolved official reference resets block returns",
            "TWSE stop-trading and full-delivery history are not complete in this restriction frame",
            "sector cap, sizing, executable T+1 price/cost, and broker lot split remain unimplemented",
            "therefore F0 overall and backward-holdout performance remain closed",
        ],
        "monthly": monthly,
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-id", default="tw_stock_data_2005_2014_r1")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "reports/mom1_release_diagnostic.json"
    )
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    document = run(args.release_id)
    _write_json(output, document)
    print(json.dumps({
        "output": str(output.resolve()),
        "sha256": sha256_file(output),
        "data_release_id": document["data_release_id"],
        "git_commit": document["git_commit"],
        "decision_months": document["coverage"]["decision_months"],
        "performance_inspected": False,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
