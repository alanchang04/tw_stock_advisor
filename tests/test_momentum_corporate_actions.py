"""MOM-1 公司行動股數政策的單元測試（合成 fixtures）。"""
from __future__ import annotations

import pandas as pd
import pytest

from research.momentum_corporate_actions import (
    apply_corporate_action,
    classify_ledger,
)


def test_executable_event_applies_multiplier_and_cash():
    result = apply_corporate_action(
        shares=1000,
        event={"ledger_status": "executable",
               "share_multiplier": 1.2, "cash_per_old_share": 2.0},
    )
    assert result.shares == 1200
    assert result.cash_delta == pytest.approx(2000.0)
    assert result.outcome == "applied"


def test_executable_event_truncates_to_whole_shares():
    """畸零部分依 D3 契約另存 entitlement，不灌回股數。"""
    result = apply_corporate_action(
        shares=1005,
        event={"ledger_status": "executable", "share_multiplier": 1.049997},
    )
    assert result.shares == 1055          # floor(1005 * 1.049997)


def test_executable_without_multiplier_is_rejected_not_defaulted_to_one():
    with pytest.raises(ValueError, match="share_multiplier"):
        apply_corporate_action(
            shares=1000,
            event={"ledger_status": "executable", "share_multiplier": None},
        )


@pytest.mark.parametrize("reason", [
    "paid_subscription_terms_missing",
    "paid_subscription_settlement_timing_missing",
])
def test_policy_a_declines_subscription_without_needing_the_terms(reason):
    """政策 A：不認購。股數不變、不付款，且完全不需要缺失的認購條件。"""
    result = apply_corporate_action(
        shares=1000, event={"ledger_status": "blocked", "ledger_block_reason": reason},
    )
    assert result.shares == 1000
    assert result.cash_delta == 0.0
    assert result.outcome == "declined_subscription"
    assert not result.position_closed


def test_policy_b_closes_position_when_mandatory_multiplier_is_unknown():
    """無償配股是強制的，股數必變，不能比照政策 A 迴避。"""
    result = apply_corporate_action(
        shares=1000,
        event={"ledger_status": "blocked",
               "ledger_block_reason": "official_free_share_terms_missing"},
        pre_event_close=82.7,
    )
    assert result.shares == 0
    assert result.cash_delta == pytest.approx(82_700.0)
    assert result.outcome == "forced_close"
    assert result.position_closed


def test_policy_b_refuses_to_close_without_a_price():
    """缺價不得假成交。"""
    with pytest.raises(ValueError, match="事件前收盤價"):
        apply_corporate_action(
            shares=1000,
            event={"ledger_status": "blocked",
                   "ledger_block_reason": "official_free_share_terms_missing"},
            pre_event_close=None,
        )


def test_unknown_ledger_status_is_rejected():
    with pytest.raises(ValueError, match="ledger_status"):
        apply_corporate_action(shares=1000, event={"ledger_status": "maybe"})


def test_negative_shares_are_rejected():
    with pytest.raises(ValueError, match="shares"):
        apply_corporate_action(shares=-1, event={"ledger_status": "executable",
                                                 "share_multiplier": 1.0})


def test_declining_never_increases_shares_so_it_cannot_overstate_returns():
    """政策 A 的方向必須是保守的：放棄認購只會低估報酬。"""
    before = 5000
    result = apply_corporate_action(
        shares=before,
        event={"ledger_status": "blocked",
               "ledger_block_reason": "paid_subscription_terms_missing"},
    )
    assert result.shares <= before
    assert result.cash_delta <= 0.0 or result.cash_delta == 0.0


def test_classify_ledger_labels_every_row():
    events = pd.DataFrame({
        "ledger_status": ["executable", "blocked", "blocked"],
        "ledger_block_reason": [None, "paid_subscription_terms_missing",
                                "official_free_share_terms_missing"],
    })
    out = classify_ledger(events)
    assert list(out["policy_outcome"]) == [
        "applied", "declined_subscription", "forced_close"]


def test_classify_ledger_rejects_missing_columns():
    with pytest.raises(ValueError, match="ledger"):
        classify_ledger(pd.DataFrame({"ledger_status": ["executable"]}))
