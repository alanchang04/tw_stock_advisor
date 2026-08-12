import pandas as pd
import pytest

from research.mops_dividend_distribution import (
    match_mops_terms_to_events,
    parse_mops_dividend_html,
)


def terms(**overrides):
    base = {
        "distribution_year_roc": 94,
        "stock_id": "2330",
        "cash_per_old_share": 1.9998,
        "stock_dividend_value_per_old_share": 0.49997,
        "free_share_multiplier": 1.049997,
    }
    return {**base, **overrides}


def event(**overrides):
    base = {
        "stock_id": "2330",
        "event_date": "2005-06-13",
        "event_kind": "ex_right_dividend",
        "pre_event_close": 57.5,
        "ex_right_reference_price": (57.5 - 1.9998) / 1.049997,
    }
    return {**base, **overrides}


def test_parse_legacy_mops_shareholder_dividend_columns():
    html = """
    <table>
      <tr><th rowspan="2">公司代號 名稱</th><th colspan="2">股東股利</th>
          <th rowspan="2">資本公積 轉增資 (元/股)</th></tr>
      <tr><th>現金股利 (元/股)</th><th>盈餘配股 (元/股)</th></tr>
      <tr><td>2330 - 台積電</td><td>1.9998</td><td>0.49997</td><td>0</td></tr>
    </table>
    """.encode("cp950")

    row = parse_mops_dividend_html(html, 94).iloc[0]

    assert row["stock_id"] == "2330"
    assert row["cash_per_old_share"] == pytest.approx(1.9998)
    assert row["free_share_multiplier"] == pytest.approx(1.049997)


def test_parse_new_mops_split_cash_and_stock_components():
    html = """
    <table>
      <tr><th rowspan="2">公司代號 名稱</th><th colspan="4">股東配發內容</th></tr>
      <tr><th>盈餘分配 之現金股利 (元/股)</th>
          <th>法定盈餘 公積、資本 公積發放 之現金(元/股)</th>
          <th>盈餘轉 增資配股 (元/股)</th>
          <th>法定盈餘 公積、資本 公積轉增資 配股(元/股)</th></tr>
      <tr><td>1234 - 測試</td><td>1.2</td><td>0.3</td><td>0.4</td><td>0.1</td></tr>
    </table>
    """.encode("cp950")

    row = parse_mops_dividend_html(html, 103).iloc[0]

    assert row["cash_per_old_share"] == pytest.approx(1.5)
    assert row["stock_dividend_value_per_old_share"] == pytest.approx(0.5)
    assert row["free_share_multiplier"] == pytest.approx(1.05)


def test_combined_event_matches_declared_cash_and_actual_share_multiplier():
    result = match_mops_terms_to_events(
        pd.DataFrame([event()]), pd.DataFrame([terms()])
    ).iloc[0]

    assert result["mops_match_status"] == "matched_unique"
    assert result["mops_cash_per_old_share"] == pytest.approx(1.9998)
    assert result["mops_free_share_multiplier"] == pytest.approx(1.049997)


def test_pure_ex_right_ignores_cash_that_was_distributed_on_another_date():
    result = match_mops_terms_to_events(
        pd.DataFrame([event(
            event_kind="ex_right",
            ex_right_reference_price=57.5 / 1.049997,
        )]),
        pd.DataFrame([terms(cash_per_old_share=7.0)]),
    ).iloc[0]

    assert result["mops_match_status"] == "matched_unique"
    assert result["mops_free_share_multiplier"] == pytest.approx(1.049997)


def test_different_economic_candidates_are_not_guessed():
    candidates = pd.DataFrame([
        terms(cash_per_old_share=2.0, free_share_multiplier=1.05),
        terms(cash_per_old_share=2.5, free_share_multiplier=1.04),
    ])
    ambiguous_reference = (57.5 - 2.0) / 1.05
    candidates.loc[1, "cash_per_old_share"] = 57.5 - ambiguous_reference * 1.04
    result = match_mops_terms_to_events(
        pd.DataFrame([event(ex_right_reference_price=ambiguous_reference)]), candidates
    ).iloc[0]

    assert result["mops_match_status"] == "reference_equation_ambiguous"
    assert result["mops_candidate_count"] == 2


def test_old_employee_bonus_dilution_can_match_by_declared_cash_and_year():
    result = match_mops_terms_to_events(
        pd.DataFrame([event(
            pre_event_close=58.1,
            ex_right_reference_price=52.88,
            cash_value=1.9998,
        )]),
        pd.DataFrame([terms()]),
    ).iloc[0]

    assert result["mops_match_status"] == "matched_unique"
    assert result["mops_match_method"] == "declared_cash_and_decision_year"
    assert result["mops_free_share_multiplier"] == pytest.approx(1.049997)
