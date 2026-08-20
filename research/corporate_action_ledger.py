"""Convert official reference-price events into actual-share ledger instructions.

An adjustment factor is sufficient for a synthetic total-return price series, but
not for an auditable position ledger.  This module only marks an event executable
when its cash and share-count effects can be separated from official fields.
"""
from __future__ import annotations

import math

import pandas as pd


REFERENCE_TOLERANCE = 0.011


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _blocked(reason: str) -> dict:
    return {
        "ledger_status": "blocked",
        "ledger_block_reason": reason,
        "cash_per_old_share": None,
        "share_multiplier": None,
        "capital_contribution_per_old_share": None,
        "economic_value_continuity_error": None,
        "ledger_terms_source": None,
        "total_return_eligible": False,
        "share_semantics": "actual_shares",
    }


def decompose_action_for_ledger(event: dict | pd.Series) -> dict:
    """Return cash/share instructions, or an explicit reason the event is blocked."""
    row = event.to_dict() if isinstance(event, pd.Series) else dict(event)
    kind = str(row.get("event_kind") or "")
    pre_close = _number(row.get("pre_event_close"))
    reference = _number(row.get("reference_price"))
    ex_right_reference = _number(row.get("ex_right_reference_price"))
    cash = _number(row.get("cash_value"))
    mops_match = str(row.get("mops_match_status") or "")
    mops_cash = _number(row.get("mops_cash_per_old_share"))
    mops_multiplier = _number(row.get("mops_free_share_multiplier"))

    if not pre_close or pre_close <= 0 or not reference or reference <= 0:
        return _blocked("missing_positive_pre_close_or_reference_price")

    share_multiplier: float | None = None
    cash_per_old_share: float | None = None
    terms_source: str | None = None

    if kind == "ex_dividend":
        if cash is None or cash < 0:
            return _blocked("cash_dividend_missing")
        share_multiplier = 1.0
        cash_per_old_share = cash
        terms_source = "twse_twt49u_declared_cash"

    elif kind in {"ex_right", "ex_right_dividend"}:
        if ex_right_reference is None or ex_right_reference <= 0:
            return _blocked("free_share_reference_price_missing")
        if abs(reference - ex_right_reference) > REFERENCE_TOLERANCE:
            # The official formula says the main reference can include paid-rights
            # subscription price and ratio.  Those terms are not present in TWT49U.
            if str(row.get("subscription_match_status") or "") == "matched_unique":
                return _blocked("paid_subscription_settlement_timing_missing")
            return _blocked("paid_subscription_terms_missing")
        if mops_match != "matched_unique" or mops_multiplier is None:
            if kind == "ex_right_dividend" and (cash is None or cash < 0):
                return _blocked("cash_stock_value_split_missing")
            if mops_match == "reference_equation_ambiguous":
                return _blocked("official_free_share_terms_ambiguous")
            return _blocked("official_free_share_terms_missing")
        if kind == "ex_right":
            cash_per_old_share = 0.0
        else:
            if mops_cash is None or mops_cash < 0:
                return _blocked("cash_stock_value_split_missing")
            cash_per_old_share = mops_cash
        share_multiplier = mops_multiplier
        terms_source = "mops_t05st09sub_declared_dividend_terms"

    elif kind == "capital_reduction":
        reason = str(row.get("reduction_reason") or "")
        if reason == "彌補虧損":
            # A price ratio is not an actual share-count ratio.  The official
            # reduction percentage is required before touching positions.
            return _blocked("capital_reduction_actual_share_ratio_missing")
        elif reason == "退還股款":
            return _blocked("capital_refund_and_exchange_ratio_missing")
        else:
            return _blocked("unsupported_capital_reduction_terms")

    else:
        return _blocked("unsupported_event_kind")

    if share_multiplier is None or not math.isfinite(share_multiplier) or share_multiplier <= 0:
        return _blocked("invalid_share_multiplier")

    continuity_error = abs(
        share_multiplier * reference + cash_per_old_share - pre_close
    )
    if continuity_error > 0.02 and terms_source != "mops_t05st09sub_declared_dividend_terms":
        return _blocked("economic_value_continuity_failed")

    return {
        "ledger_status": "executable",
        "ledger_block_reason": None,
        "cash_per_old_share": cash_per_old_share,
        "share_multiplier": share_multiplier,
        "capital_contribution_per_old_share": 0.0,
        "economic_value_continuity_error": continuity_error,
        "ledger_terms_source": terms_source,
        "total_return_eligible": True,
        "share_semantics": "actual_shares",
    }


def build_execution_action_ledger(events: pd.DataFrame) -> pd.DataFrame:
    """Append conservative actual-share ledger eligibility to corporate actions."""
    if events.empty:
        result = events.copy()
        empty_columns = {
            "ledger_status": "object",
            "ledger_block_reason": "object",
            "cash_per_old_share": "float64",
            "share_multiplier": "float64",
            "capital_contribution_per_old_share": "float64",
            "economic_value_continuity_error": "float64",
            "ledger_terms_source": "object",
            "total_return_eligible": "bool",
            "share_semantics": "object",
        }
        for column, dtype in empty_columns.items():
            result[column] = pd.Series(index=result.index, dtype=dtype)
        return result
    decomposed = pd.DataFrame(
        [decompose_action_for_ledger(row) for _, row in events.iterrows()],
        index=events.index,
    )
    return pd.concat([events.copy(), decomposed], axis=1)


def apply_action_to_position(
    shares: int,
    cash: float,
    instruction: dict | pd.Series,
    fractional_cash_price: float | None = None,
) -> dict:
    """Apply an executable event without ever storing synthetic units as shares.

    Fractional entitlements remain separate and non-tradable until an official
    cash-in-lieu price is supplied.  ``shares`` therefore stays an integer.
    """
    if isinstance(shares, bool) or not isinstance(shares, int) or shares < 0:
        raise ValueError("shares must be a non-negative integer number of shares")
    if not math.isfinite(float(cash)):
        raise ValueError("cash must be finite")
    row = instruction.to_dict() if isinstance(instruction, pd.Series) else dict(instruction)
    if row.get("ledger_status") != "executable":
        raise ValueError(f"corporate action is not executable: {row.get('ledger_block_reason')}")

    multiplier = _number(row.get("share_multiplier"))
    cash_per_old_share = _number(row.get("cash_per_old_share"))
    if multiplier is None or multiplier <= 0 or cash_per_old_share is None:
        raise ValueError("executable instruction is missing cash/share terms")

    exact_new_shares = shares * multiplier
    whole_shares = math.floor(exact_new_shares + 1e-10)
    fractional_entitlement = max(exact_new_shares - whole_shares, 0.0)
    new_cash = float(cash) + shares * cash_per_old_share
    settlement_status = "applied"
    if fractional_entitlement > 1e-9:
        settlement_price = _number(fractional_cash_price)
        if settlement_price is None or settlement_price < 0:
            settlement_status = "fractional_settlement_pending"
        else:
            new_cash += fractional_entitlement * settlement_price
            fractional_entitlement = 0.0

    return {
        "shares": int(whole_shares),
        "cash": new_cash,
        "fractional_share_entitlement": fractional_entitlement,
        "settlement_status": settlement_status,
        "share_semantics": "actual_shares",
    }


def create_subscription_entitlement(
    shares: int,
    event: dict | pd.Series,
) -> dict:
    """Create a pending paid-subscription right without changing held shares."""
    if isinstance(shares, bool) or not isinstance(shares, int) or shares < 0:
        raise ValueError("shares must be a non-negative integer number of shares")
    row = event.to_dict() if isinstance(event, pd.Series) else dict(event)
    if row.get("subscription_match_status") != "matched_unique":
        raise ValueError("paid-subscription terms do not have a unique official match")
    rate = _number(row.get("official_subscription_rate"))
    price = _number(row.get("official_subscription_price"))
    dilution_rate = _number(row.get("official_paid_dilution_rate"))
    if rate is None or rate <= 0 or price is None or price <= 0:
        raise ValueError("matched subscription is missing a positive entitlement rate or price")

    exact_entitlement = shares * rate
    whole_entitlement = math.floor(exact_entitlement + 1e-10)
    fractional_entitlement = max(exact_entitlement - whole_entitlement, 0.0)
    return {
        "current_shares": shares,
        "subscription_entitlement_shares": int(whole_entitlement),
        "fractional_subscription_entitlement": fractional_entitlement,
        "subscription_price_per_share": price,
        "cash_required_for_whole_entitlement": whole_entitlement * price,
        "paid_issue_dilution_rate": dilution_rate,
        "settlement_status": "pending_payment_and_new_share_delivery",
        "share_semantics": "actual_shares_unchanged_until_delivery",
    }
