import pandas as pd

from scripts.audit_twse_corporate_action_jumps import build_jump_audit


def test_jump_audit_separates_event_long_gap_and_unexplained():
    prices = pd.DataFrame([
        {"stock_id": "1101", "trade_date": "2012-01-02", "open": 100.0, "close": 100.0},
        {"stock_id": "1101", "trade_date": "2012-01-03", "open": 49.0, "close": 50.0},
        {"stock_id": "2201", "trade_date": "2012-01-02", "open": 100.0, "close": 100.0},
        {"stock_id": "2201", "trade_date": "2012-02-02", "open": 70.0, "close": 70.0},
        {"stock_id": "3301", "trade_date": "2012-01-02", "open": 100.0, "close": 100.0},
        {"stock_id": "3301", "trade_date": "2012-01-03", "open": 70.0, "close": 70.0},
    ])
    events = pd.DataFrame([{
        "source_report": "TWT49U",
        "stock_id": "1101",
        "event_date": "2012-01-03",
        "event_kind": "ex_right",
        "pre_event_close": 100.0,
        "reference_price": 50.0,
        "adjustment_factor": 0.5,
    }])

    report, jumps, event_checks = build_jump_audit(prices, events)

    assert report["jump_classifications"] == {
        "long_observation_gap_manual_review": 1,
        "matched_corporate_action": 1,
        "unexplained_short_gap": 1,
    }
    assert not report["passed_no_unexplained_short_gap"]
    assert jumps.loc[jumps["stock_id"].eq("1101"), "official_pre_close_matches"].item()
    assert event_checks["has_price_on_effective_date"].item()


def test_jump_audit_classifies_non_common_and_new_listing():
    prices = pd.DataFrame([
        {"stock_id": "0080", "trade_date": "2012-01-02", "open": 100.0, "close": 100.0},
        {"stock_id": "0080", "trade_date": "2012-01-03", "open": 120.0, "close": 130.0},
        {"stock_id": "2634", "trade_date": "2014-08-25", "open": 30.0, "close": 30.0},
        {"stock_id": "2634", "trade_date": "2014-08-26", "open": 31.0, "close": 38.5},
    ])
    events = pd.DataFrame(columns=[
        "source_report", "stock_id", "event_date", "event_kind", "pre_event_close",
        "reference_price", "adjustment_factor",
    ])
    master = pd.DataFrame([
        {"stock_id": "0080", "asset_type": "non_company_security", "listing_date": None},
        {"stock_id": "2634", "asset_type": "common_stock", "listing_date": "2014-08-25"},
    ])

    report, jumps, _ = build_jump_audit(prices, events, security_master=master)

    assert report["jump_classifications"] == {
        "excluded_non_common_security": 1,
        "new_listing_price_discovery": 1,
    }
    assert report["passed_no_unexplained_short_gap"]
