import pandas as pd
import pytest

from research.corporate_action_ledger import (
    apply_action_to_position,
    build_execution_action_ledger,
    create_subscription_entitlement,
    decompose_action_for_ledger,
)


def event(**overrides):
    base = {
        "event_kind": "ex_dividend",
        "pre_event_close": 100.0,
        "reference_price": 97.0,
        "ex_right_reference_price": 97.0,
        "cash_value": 3.0,
        "reduction_reason": None,
        "mops_match_status": "matched_unique",
        "mops_cash_per_old_share": 3.0,
        "mops_free_share_multiplier": 1.0,
    }
    return {**base, **overrides}


def test_pure_cash_dividend_keeps_actual_shares_and_credits_cash():
    result = decompose_action_for_ledger(event())

    assert result["ledger_status"] == "executable"
    assert result["share_multiplier"] == 1.0
    assert result["cash_per_old_share"] == 3.0
    assert result["share_semantics"] == "actual_shares"


def test_free_stock_dividend_changes_actual_share_count():
    result = decompose_action_for_ledger(event(
        event_kind="ex_right", reference_price=80.0,
        ex_right_reference_price=80.0, cash_value=0.0,
        mops_free_share_multiplier=1.25,
    ))

    assert result["ledger_status"] == "executable"
    assert result["share_multiplier"] == pytest.approx(1.25)
    assert result["cash_per_old_share"] == 0.0


def test_paid_subscription_rights_are_blocked_without_terms():
    result = decompose_action_for_ledger(event(
        event_kind="ex_right", reference_price=90.0,
        ex_right_reference_price=100.0, cash_value=0.0,
    ))

    assert result["ledger_status"] == "blocked"
    assert result["ledger_block_reason"] == "paid_subscription_terms_missing"
    assert not result["total_return_eligible"]


def test_matched_subscription_stays_blocked_until_payment_and_delivery():
    source = event(
        event_kind="ex_right", reference_price=90.0,
        ex_right_reference_price=100.0, cash_value=0.0,
        subscription_match_status="matched_unique",
        official_subscription_rate=0.075,
        official_subscription_price=20.0,
        official_paid_dilution_rate=0.1,
    )
    instruction = decompose_action_for_ledger(source)
    entitlement = create_subscription_entitlement(1_003, source)

    assert instruction["ledger_status"] == "blocked"
    assert instruction["ledger_block_reason"] == "paid_subscription_settlement_timing_missing"
    assert entitlement["current_shares"] == 1_003
    assert entitlement["subscription_entitlement_shares"] == 75
    assert entitlement["fractional_subscription_entitlement"] == pytest.approx(0.225)
    assert entitlement["cash_required_for_whole_entitlement"] == 1_500.0
    assert entitlement["settlement_status"] == "pending_payment_and_new_share_delivery"


def test_combined_event_is_blocked_when_cash_stock_split_is_missing():
    result = decompose_action_for_ledger(event(
        event_kind="ex_right_dividend", reference_price=80.0,
        ex_right_reference_price=80.0, cash_value=None,
        mops_match_status="reference_equation_no_match",
    ))

    assert result["ledger_status"] == "blocked"
    assert result["ledger_block_reason"] == "cash_stock_value_split_missing"


def test_reductions_stay_blocked_until_actual_share_or_refund_terms_exist():
    loss = decompose_action_for_ledger(event(
        event_kind="capital_reduction", pre_event_close=20.0,
        reference_price=25.0, reduction_reason="彌補虧損", cash_value=None,
    ))
    refund = decompose_action_for_ledger(event(
        event_kind="capital_reduction", pre_event_close=20.0,
        reference_price=25.0, reduction_reason="退還股款", cash_value=None,
    ))

    assert loss["ledger_status"] == "blocked"
    assert loss["ledger_block_reason"] == "capital_reduction_actual_share_ratio_missing"
    assert refund["ledger_status"] == "blocked"
    assert refund["ledger_block_reason"] == "capital_refund_and_exchange_ratio_missing"


def test_dataframe_builder_preserves_source_rows_and_adds_gate_columns():
    source = pd.DataFrame([event(), event(event_kind="unknown")])

    result = build_execution_action_ledger(source)

    assert len(result) == len(source)
    assert result["ledger_status"].tolist() == ["executable", "blocked"]
    assert result["total_return_eligible"].tolist() == [True, False]


def test_position_application_keeps_shares_integer_and_cash_separate():
    instruction = decompose_action_for_ledger(event(
        event_kind="ex_right_dividend", pre_event_close=100.0,
        reference_price=80.0, ex_right_reference_price=80.0, cash_value=4.0,
        mops_cash_per_old_share=4.0, mops_free_share_multiplier=1.2,
    ))

    result = apply_action_to_position(1_000, 5_000.0, instruction)

    assert result["shares"] == 1_200
    assert isinstance(result["shares"], int)
    assert result["cash"] == 9_000.0
    assert result["fractional_share_entitlement"] == 0.0


def test_fractional_entitlement_never_masquerades_as_tradable_shares():
    instruction = decompose_action_for_ledger(event(
        event_kind="ex_right", pre_event_close=100.0,
        reference_price=80.0, ex_right_reference_price=80.0, cash_value=0.0,
        mops_free_share_multiplier=1.25,
    ))

    pending = apply_action_to_position(123, 0.0, instruction)
    settled = apply_action_to_position(123, 0.0, instruction, fractional_cash_price=82.0)

    assert pending["shares"] == 153
    assert pending["fractional_share_entitlement"] == pytest.approx(0.75)
    assert pending["settlement_status"] == "fractional_settlement_pending"
    assert settled["shares"] == 153
    assert settled["fractional_share_entitlement"] == 0.0
    assert settled["cash"] == pytest.approx(61.5)
