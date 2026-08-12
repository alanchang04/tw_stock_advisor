"""Diagnose MOM-1 sector/sizing/T+1 F0 readiness without calculating returns."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.momentum import (  # noqa: E402
    compute_mom_6_1,
    eligible_universe,
    month_end_sessions,
    next_session,
    pit_common_stock_mask,
    select_holdings,
)
from research.momentum_execution import (  # noqa: E402
    build_equal_weight_rebalance_orders,
    pit_industry_map,
    select_holdings_with_industry_cap,
)
from research.momentum_release import load_momentum_release, sha256_file  # noqa: E402


STRATEGY_PATHS = (
    "research/momentum.py",
    "research/momentum_execution.py",
    "research/momentum_release.py",
    "scripts/diagnose_mom1_f0_execution.py",
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


def run(release_id: str) -> dict:
    dirty = _git("status", "--porcelain", "--", *STRATEGY_PATHS)
    if dirty:
        raise RuntimeError("F0 strategy contract 必須先 commit 才能產生診斷:\n" + dirty)
    strategy_commit = _git("log", "-1", "--format=%H", "--", *STRATEGY_PATHS)
    inputs = load_momentum_release(release_id, root=ROOT)
    signal = compute_mom_6_1(inputs.adjusted_close)
    decisions = month_end_sessions(inputs.trading_days)

    previous_holdings: list[str] = []
    monthly: list[dict] = []
    for decision in decisions:
        pit_mask = pit_common_stock_mask(
            inputs.pit_master, decision, inputs.adjusted_close.columns
        )
        eligible = eligible_universe(
            as_of=decision,
            adjusted_close=inputs.adjusted_close,
            raw_close=inputs.raw_close,
            turnover=inputs.turnover,
            signal=signal,
            pit_mask=pit_mask,
            restricted=inputs.restricted,
        )
        values = signal.loc[decision, eligible]
        uncapped = select_holdings(values, previous_holdings, max_positions=10)
        industries = pit_industry_map(inputs.market_structure, decision, eligible)
        uncapped_missing = int(industries.reindex(uncapped).isna().sum())
        strict_audit_error = None
        try:
            select_holdings_with_industry_cap(
                values, industries, previous_holdings, require_complete_industry=True
            )
        except ValueError as exc:
            strict_audit_error = str(exc)

        # Frozen formal policy: unknown PIT industry is ineligible, never backfilled.
        formal_selected = select_holdings_with_industry_cap(
            values, industries, previous_holdings
        )
        previous_holdings = formal_selected

        execution_date = next_session(inputs.trading_days, decision)
        open_ready = 0
        sizing_ready = 0
        sample_orders: list[dict] = []
        if execution_date is not None:
            volume_window = inputs.volume_shares.loc[:decision].tail(20)
            average_volume = volume_window.mean(skipna=False)
            for stock_id in formal_selected:
                open_price = inputs.raw_open.at[execution_date, stock_id]
                avg_volume = average_volume.get(stock_id, np.nan)
                if pd.isna(open_price) or not np.isfinite(open_price) or open_price <= 0:
                    continue
                open_ready += 1
                if pd.isna(avg_volume) or not np.isfinite(avg_volume) or avg_volume < 0:
                    continue
                orders = build_equal_weight_rebalance_orders(
                    target_holdings=[stock_id],
                    current_shares={},
                    raw_open_prices=pd.Series({stock_id: float(open_price)}),
                    average_volumes_shares=pd.Series({stock_id: float(avg_volume)}),
                    nav=300_000.0,
                )
                if not orders:
                    continue
                sizing_ready += 1
                sample_orders.append(orders[0].to_dict())

        monthly.append({
            "decision_date": decision.date().isoformat(),
            "execution_date": None if execution_date is None else execution_date.date().isoformat(),
            "eligible_count": len(eligible),
            "uncapped_selected_count": len(uncapped),
            "uncapped_selected_missing_pit_industry_count": uncapped_missing,
            "industry_cap_formal_ready": True,
            "strict_missing_industry_audit_error": strict_audit_error,
            "formal_selected_stock_ids": formal_selected,
            "tplus1_open_ready_count": open_ready,
            "sizing_ready_count": sizing_ready,
            "formal_target_orders": sample_orders,
            "official_locked_limit_state_available": False,
        })

    active = [row for row in monthly if row["eligible_count"] > 0]
    return {
        "schema_version": 1,
        "diagnostic": "MOM1 F0 portfolio construction and execution readiness",
        "performance_inspected": False,
        "data_release_id": inputs.release_id,
        "release_descriptor_sha256": inputs.descriptor_sha256,
        "verified_input_sha256": inputs.verified_input_sha256,
        "strategy_commit": strategy_commit,
        "strategy_spec_sha256": sha256_file(
            ROOT / "docs/SPEC_DATA_FOUNDATION_AND_MOMENTUM.md"
        ),
        "command": (
            ".\\.venv-repro\\Scripts\\python.exe "
            ".\\scripts\\diagnose_mom1_f0_execution.py "
            f"--release-id {release_id} --output reports\\mom1_f0_execution_readiness.json"
        ),
        "rules": {
            "maximum_positions": 10,
            "maximum_names_per_industry": 3,
            "target_weight_per_name": 0.10,
            "maximum_average_volume_fraction": 0.01,
            "capital_twd": 300000,
            "pending_order_expiry": "next monthly decision or sample end",
            "missing_industry_policy": "exclude_stock_fail_closed_never_backfill",
        },
        "summary": {
            "decision_months": len(monthly),
            "active_decision_months": len(active),
            "industry_cap_formal_ready_months": len(active),
            "strict_complete_industry_audit_failed_months": sum(
                row["strict_missing_industry_audit_error"] is not None for row in active
            ),
            "uncapped_selected_missing_industry_months": sum(
                row["uncapped_selected_missing_pit_industry_count"] > 0 for row in active
            ),
            "formal_selected_stock_months": sum(
                len(row["formal_selected_stock_ids"]) for row in active
            ),
            "tplus1_open_ready_stock_months": sum(
                row["tplus1_open_ready_count"] for row in active
            ),
            "sizing_ready_stock_months": sum(row["sizing_ready_count"] for row in active),
        },
        "f0_status": "blocked",
        "remaining_blockers": [
            "official TWSE locked-limit state is not present in the named release",
            "TWSE stop-trading has no separate official flag and is inferred only from a missing quote",
            "D3 actual-share execution ledger and unresolved reference resets block performance",
        ],
        "monthly": monthly,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-id", default="tw_stock_data_2005_2014_r1")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "reports/mom1_f0_execution_readiness.json"
    )
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    document = run(args.release_id)
    _write_json(output, document)
    print(json.dumps({
        "output": str(output.resolve()),
        "sha256": sha256_file(output),
        "strategy_commit": document["strategy_commit"],
        "f0_status": document["f0_status"],
        "performance_inspected": False,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
