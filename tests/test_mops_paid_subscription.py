import pandas as pd
import pytest

from research.mops_paid_subscription import (
    extract_subscription_facts,
    extract_subscription_settlement_facts,
    history_records,
    match_subscription_terms_to_events,
)


def test_history_rows_preserve_detail_key():
    payload = {"result": {"data": [[
        "2023", "燁輝", "94/01/04", "17:07:17", "調整現金增資認購比率",
        {"apiName": "t05st01_detail", "parameters": {
            "companyId": "2023", "marketKind": "sii",
            "enterDate": "0940104", "serialNumber": "1",
        }},
    ]]}}

    row = history_records(payload)[0]

    assert row["stock_id"] == "2023"
    assert row["announcement_date"].isoformat() == "2005-01-04"
    assert row["enter_date"] == "0940104"


def test_extracts_per_thousand_rate_and_revised_cash_issue_prices():
    text = (
        "本次現金增資認購比率調整後，每仟股認購73.039股。\n"
        "現金增資發行價格每股新台幣22元調降為每股新台幣20元。\n"
        "發行股數之75%供原股東認購。\n"
        "可轉換公司債轉換價格21.5元。"
    )

    facts = extract_subscription_facts(text)

    assert facts["subscription_rates"] == [pytest.approx(0.073039)]
    assert facts["subscription_prices"] == [20.0, 22.0]
    assert facts["shareholder_allocation_fractions"] == [0.75]


def test_reference_equation_selects_unique_official_pair():
    events = pd.DataFrame([{
        "stock_id": "1234", "event_date": "2010-06-01", "event_kind": "ex_right",
        "pre_event_close": 30.0, "reference_price": (30 + 20 * 0.1) / 1.1,
        "ex_right_reference_price": 30.0,
        "mops_cash_per_old_share": 0.0, "mops_free_share_multiplier": 1.0,
    }])
    facts = pd.DataFrame([
        {"stock_id": "1234", "announcement_date": "2010-05-01",
         "fact_type": "subscription_rate", "fact_value": 0.075},
        {"stock_id": "1234", "announcement_date": "2010-05-01",
         "fact_type": "shareholder_allocation_fraction", "fact_value": 0.75},
        {"stock_id": "1234", "announcement_date": "2010-05-02",
         "fact_type": "subscription_price", "fact_value": 20.0},
        {"stock_id": "1234", "announcement_date": "2010-05-03",
         "fact_type": "subscription_price", "fact_value": 25.0},
        {"stock_id": "1234", "announcement_date": "2010-05-04",
         "fact_type": "subscription_payment_start", "fact_value": None,
         "fact_date": "2010-06-10"},
        {"stock_id": "1234", "announcement_date": "2010-05-04",
         "fact_type": "subscription_payment_end", "fact_value": None,
         "fact_date": "2010-06-20"},
        {"stock_id": "1234", "announcement_date": "2010-06-30",
         "fact_type": "new_share_delivery_date", "fact_value": None,
         "fact_date": "2010-07-05"},
    ])

    result = match_subscription_terms_to_events(events, facts).iloc[0]

    assert result["subscription_match_status"] == "matched_unique"
    assert result["official_subscription_rate"] == pytest.approx(0.075)
    assert result["official_subscription_price"] == pytest.approx(20.0)
    assert result["official_paid_dilution_rate"] == pytest.approx(0.1)
    assert result["subscription_payment_start"] == "2010-06-10"
    assert result["new_share_delivery_date"] == "2010-07-05"
    assert result["subscription_settlement_match_status"] == "matched_payment_and_delivery"


def test_extracts_chinese_and_slash_settlement_dates_without_adding_shares():
    text = (
        "本公司訂於九十四年十一月二十七日為認股基準日。\n"
        "九十四年十一月三十日至九十四年十二月十三日為原股東及員工股款繳納期間。\n"
        "九十四年十二月十九日為現金增資之增資基準日。\n"
        "現金增資新股上市日期為95/01/10。"
    )

    facts = extract_subscription_settlement_facts(text)

    assert facts["subscription_record_date"] == ["2005-11-27"]
    assert facts["subscription_payment_start"] == ["2005-11-30"]
    assert facts["subscription_payment_end"] == ["2005-12-13"]
    assert facts["capital_increase_effective_date"] == ["2005-12-19"]
    assert facts["new_share_delivery_date"] == ["2006-01-10"]


def test_delivery_date_does_not_capture_nearby_funds_received_date():
    text = (
        "該現金增資股股款已於100/02/22收足，"
        "並於100/02/25以股款繳納憑證上市買賣。"
    )

    facts = extract_subscription_settlement_facts(text)

    assert facts["new_share_delivery_date"] == ["2011-02-25"]


def test_settlement_does_not_splice_next_issue_payment_onto_prior_delivery():
    events = pd.DataFrame([{
        "stock_id": "8011", "event_date": "2012-12-27", "event_kind": "ex_right",
        "pre_event_close": 30.0, "reference_price": (30 + 20 * 0.1) / 1.1,
        "ex_right_reference_price": 30.0,
        "mops_cash_per_old_share": 0.0, "mops_free_share_multiplier": 1.0,
    }])
    facts = pd.DataFrame([
        {"stock_id": "8011", "announcement_date": "2012-12-01",
         "fact_type": "subscription_rate", "fact_value": 0.075},
        {"stock_id": "8011", "announcement_date": "2012-12-01",
         "fact_type": "shareholder_allocation_fraction", "fact_value": 0.75},
        {"stock_id": "8011", "announcement_date": "2012-12-01",
         "fact_type": "subscription_price", "fact_value": 20.0},
        {"stock_id": "8011", "announcement_date": "2013-01-20",
         "enter_date": "1020120", "serial_number": "1",
         "fact_type": "new_share_delivery_date", "fact_value": None,
         "fact_date": "2013-01-25"},
        {"stock_id": "8011", "announcement_date": "2013-10-20",
         "enter_date": "1021020", "serial_number": "2",
         "fact_type": "subscription_payment_start", "fact_value": None,
         "fact_date": "2013-11-06"},
        {"stock_id": "8011", "announcement_date": "2013-10-20",
         "enter_date": "1021020", "serial_number": "2",
         "fact_type": "subscription_payment_end", "fact_value": None,
         "fact_date": "2013-11-13"},
    ])

    result = match_subscription_terms_to_events(events, facts).iloc[0]

    assert result["subscription_match_status"] == "matched_unique"
    assert result["subscription_payment_start"] is None
    assert result["new_share_delivery_date"] == "2013-01-25"
    assert result["subscription_settlement_match_status"] == "matched_delivery_payment_missing"
