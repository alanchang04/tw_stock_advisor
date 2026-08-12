from datetime import date

import pandas as pd

from research.tpex_history import (
    parse_attention,
    parse_daily_quotes,
    parse_delisted,
    parse_disposal,
    parse_ex_daily,
    parse_legacy_altered_html,
    parse_trading_restrictions,
    roc_date,
)


def table(fields, data, title=""):
    return {"stat": "ok", "tables": [{"title": title, "fields": fields, "data": data}]}


def test_roc_date():
    assert roc_date("97/01/02") == date(2008, 1, 2)
    assert roc_date("103-12-31") == date(2014, 12, 31)
    assert roc_date("bad") is None


def test_daily_quotes_filters_instruments_and_retains_suspended_row():
    fields = ["代號", "名稱", "收盤", "開盤", "最高", "最低", "成交股數", "成交金額(元)", "發行股數"]
    payload = table(fields, [
        ["1333", "恩得利", "14.20", "14.00", "14.65", "14.00", "386,670", "5,530,993", "83,025,000"],
        ["01006T", "基泰SR", "7.63", "7.65", "7.65", "7.60", "395,000", "3,010,960", "247,000,000"],
        ["4609", "唐鋒", "----", "----", "----", "----", "0", "0", "73,000,000"],
        ["5201", "凱衛", "0.00", "0.00", "0.00", "0.00", "0", "0", "63,000,000"],
    ], "上櫃股票行情")
    result = parse_daily_quotes(payload, date(2008, 1, 2))
    assert list(result.stock_id) == ["1333", "4609", "5201"]
    assert result.loc[result.stock_id.eq("1333"), "volume"].iat[0] == 386_670
    suspended = result[result.stock_id.eq("4609")].iloc[0]
    assert pd.isna(suspended.close)
    assert bool(suspended.price_missing)
    assert suspended.volume == 0
    assert suspended.turnover == 0
    assert suspended.volume_unit == "shares"
    assert pd.isna(result[result.stock_id.eq("5201")].iloc[0].close)


def test_ex_daily_keeps_right_value_unit_distinct_from_share_ratio():
    fields = ["除權息日期", "代號", "名稱", "除權息前收盤價", "除權息參考價", "權值", "息值", "權/息"]
    result = parse_ex_daily(table(fields, [["97/01/10", "8097", "鴻松", "11.80", "11.46", "0.34", "0", "除權"]]))
    row = result.iloc[0]
    assert row.stock_dividend_value == 0.34
    assert row.stock_dividend_value_unit == "TWD_per_share_reference_deduction"


def test_delisted_attention_and_disposal_parsers():
    delisted = parse_delisted(table(
        ["股票代號", "公司名稱", "終止上櫃日期", "終止上櫃原因", "公司資料網址"],
        [["6108", "競國", "97-12-30", "轉上市", "url"]],
    ))
    assert delisted.iloc[0].delisting_date == "2008-12-30"

    attention = parse_attention(table(
        ["編號", "證券代號", "證券名稱", "累計", "注意交易資訊", "公告日期", "收盤價", "本益比"],
        [["1", "3268", "海德威", "4", "價格異常", "97/01/31", "31.55", "23.37"]],
    ))
    assert attention.iloc[0].notice_date == "2008-01-31"

    disposal = parse_disposal(table(
        ["編號", "公布日期", "證券代號", "證券名稱", "累計", "處置起訖時間", "處置原因", "處置內容", "收盤價", "本益比"],
        [["11", "97/12/17", "9951", "皇田(link)", "1", "97/12/18~97/12/24", "連續5日", "每5分鐘撮合", "7.29", "2.84"]],
    ))
    row = disposal.iloc[0]
    assert row.start_date == "2008-12-18"
    assert row.end_date == "2008-12-24"
    assert row.stock_name == "皇田(link)"


def test_modern_and_legacy_trading_restrictions_keep_unknown_semantics():
    modern = parse_trading_restrictions(table(
        ["證券代號", "證券名稱", "變更交易", "分盤交易", "屬管理股票",
         "分盤或管理股票撮合循環時間(分鐘)", "停止交易", "財務資訊重點專區"],
        [["4609", "唐鋒", "Ｙ", "", "", "", "Ｙ", "Ｙ"]],
    ), date(2014, 12, 31))
    assert bool(modern.iloc[0].altered_trading)
    assert bool(modern.iloc[0].suspended)
    assert not bool(modern.iloc[0].managed_stock)

    raw = b"<TR><TD class='x'>4413</TD><TD class='x'>NAME</TD></TR>"
    legacy = parse_legacy_altered_html(raw, date(2008, 1, 2))
    assert bool(legacy.iloc[0].altered_trading)
    assert pd.isna(legacy.iloc[0].suspended)
    assert "other_flags_unknown" in legacy.iloc[0].source_detail
