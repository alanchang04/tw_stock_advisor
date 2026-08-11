from datetime import date

import pytest

from scripts.backfill_twse_corporate_actions import (
    REPORT_EX_RIGHT,
    REPORT_REDUCTION,
    _roc_date,
    connect,
    parse_twt49u,
    parse_twtauu,
    upsert_period,
    write_raw_response,
)


def test_roc_date_supports_chinese_and_slash_formats():
    assert _roc_date("94年01月11日") == date(2005, 1, 11)
    assert _roc_date("100/01/25") == date(2011, 1, 25)


def test_parse_twt49u_uses_named_fields_and_builds_adjustment_factor():
    payload = {
        "stat": "OK",
        "fields": ["資料日期", "股票代號", "股票名稱", "除權息前收盤價", "除權息參考價",
                   "權值", "息值", "權值+息值", "權/息", "開盤競價基準", "減除股利參考價"],
        "data": [["94年01月11日", "6280", "崇貿", "33.00", "27.48", 5.52, 0,
                  "5.520000", "權", "27.50", "27.48"]],
    }
    row = parse_twt49u(payload).iloc[0]
    assert row["source_report"] == REPORT_EX_RIGHT
    assert row["event_kind"] == "ex_right"
    assert row["rights_value"] == pytest.approx(5.52)
    assert row["cash_value"] == pytest.approx(0.0)
    assert row["adjustment_factor"] == pytest.approx(27.48 / 33.0)


def test_parse_twtauu_preserves_reason_and_reference_factor():
    payload = {
        "stat": "OK",
        "fields": ["恢復買賣日期", "股票代號", "名稱", "停止買賣前收盤價格", "恢復買賣參考價",
                   "開盤競價基準", "除權參考價", "減資原因", "詳細資料"],
        "data": [["100/01/25", "2412", "中華電", "73.10", "88.87", "88.90", "--",
                  "退還股款", "2412,20110128"]],
    }
    row = parse_twtauu(payload).iloc[0]
    assert row["source_report"] == REPORT_REDUCTION
    assert row["event_kind"] == "capital_reduction"
    assert row["reduction_reason"] == "退還股款"
    assert row["adjustment_factor"] == pytest.approx(88.87 / 73.10)


def test_source_rows_preserve_aliases_while_canonical_event_is_deduplicated(tmp_path):
    payload = {
        "stat": "OK",
        "fields": ["恢復買賣日期", "股票代號", "名稱", "停止買賣前收盤價格", "恢復買賣參考價",
                   "減資原因", "詳細資料"],
        "data": [
            ["103/09/09", "2408", "南亞科", "8.1", "80.93", "彌補虧損", "2408,20140922"],
            ["103/09/09", "2408", "南科", "8.1", "80.93", "彌補虧損", "2408,20140820"],
        ],
    }
    archive = tmp_path / "TWTAUU_201409.json.gz"
    write_raw_response(archive, payload)
    frame = parse_twtauu(payload)
    connection = connect(tmp_path / "events.sqlite3")
    try:
        upsert_period(
            connection, REPORT_REDUCTION, date(2014, 9, 1), date(2014, 9, 30),
            payload, frame, archive,
        )
        source_count = connection.execute(
            "SELECT COUNT(*) FROM corporate_action_source_rows"
        ).fetchone()[0]
        event_count = connection.execute(
            "SELECT COUNT(*) FROM corporate_action_events"
        ).fetchone()[0]
    finally:
        connection.close()
    assert source_count == 2
    assert event_count == 1
