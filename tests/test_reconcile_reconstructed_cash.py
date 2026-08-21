from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from scripts.reconcile_reconstructed_paper_cash import (
    RECONSTRUCTED_CASH_MARKER,
    missing_cash_debit,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "reconcile_reconstructed_paper_cash.py"
SOURCE = SCRIPT.read_text(encoding="utf-8")


def test_full_missing_debit_is_reported():
    assert missing_cash_debit(Decimal("283188.46"), Decimal("0")) == Decimal("283188.46")


def test_recorded_debit_makes_repair_idempotent():
    assert missing_cash_debit(
        Decimal("283188.46"), Decimal("283188.46")
    ) == Decimal("0.00")


def test_incremental_reconstructed_cost_only_repairs_difference():
    assert missing_cash_debit(
        Decimal("300000.00"), Decimal("283188.46")
    ) == Decimal("16811.54")


def test_over_debited_ledger_is_rejected():
    with pytest.raises(ValueError, match="大於 reconstructed 成本"):
        missing_cash_debit(Decimal("100"), Decimal("101"))


def test_script_is_dry_run_by_default_and_locks_on_commit():
    assert '"--commit"' in SOURCE
    assert "lock=args.commit" in SOURCE
    assert "FOR UPDATE" in SOURCE


def test_repair_has_a_stable_audit_marker_and_adjustment_entry():
    assert RECONSTRUCTED_CASH_MARKER == "reconstructed_cash_debit_v1"
    assert "kind='adjustment'" in SOURCE
    assert "amount < 0" in SOURCE
    assert "UPDATE paper_accounts" in SOURCE


def test_cost_query_includes_closed_reconstructed_positions():
    query_start = SOURCE.index("SELECT id, stock_id, entry_cost")
    query_end = SOURCE.index('"""), {"aid": account_id}', query_start)
    cost_query = SOURCE[query_start:query_end]
    assert "shares_source='reconstructed'" in cost_query
    assert "status='open'" not in cost_query
