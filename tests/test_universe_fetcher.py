import datetime as dt

from data_pipeline.fetchers.universe_fetcher import (
    _parse_company_rows,
    _parse_listing_date,
)


def test_parse_listing_date_accepts_ad_and_roc_formats():
    assert _parse_listing_date("2024/01/02") == dt.date(2024, 1, 2)
    assert _parse_listing_date("113/01/02") == dt.date(2024, 1, 2)
    assert _parse_listing_date("20240102") == dt.date(2024, 1, 2)


def test_parse_company_rows_keeps_only_four_digit_common_stocks():
    rows = [
        {"公司代號": "2330", "公司簡稱": "台積電", "上市日期": "83/09/05"},
        {"公司代號": "0050", "公司簡稱": "ETF", "上市日期": "92/06/30"},
        {"公司代號": "ABC", "公司簡稱": "bad", "上市日期": "2024/01/01"},
    ]
    out = _parse_company_rows(rows, "TWSE")
    assert out["stock_id"].tolist() == ["2330", "0050"]
    assert out.iloc[0]["listing_date"] == dt.date(1994, 9, 5)
    assert (out["asset_type"] == "common_stock").all()
