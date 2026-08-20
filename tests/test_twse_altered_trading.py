"""TWSE 變更交易方法正規化的品質測試（合成 fixtures，不讀 snapshot）。"""
from __future__ import annotations

import pandas as pd
import pytest

from research.twse_altered_trading import (
    normalize_payload,
    parse_title_date,
    restriction_frame,
    schema_of,
)


def payload(title, fields, data, stat="OK"):
    return {"stat": stat, "title": title, "fields": fields, "data": data}


LATE = ["證券代號", "證券名稱", "分盤集合競價(以**表示)"]
EARLY = ["證券代號", "證券名稱"]


def test_parse_title_handles_roc_year():
    assert parse_title_date("094年01月03日 全額交割證券") == pd.Timestamp("2005-01-03")
    assert parse_title_date("103年12月31日 變更交易") == pd.Timestamp("2014-12-31")


def test_schema_detection_distinguishes_eras():
    assert schema_of(LATE) == "with_periodic_call_auction"
    assert schema_of(EARLY) == "code_name_only"
    assert schema_of(["某個沒看過的欄位"]) == "unknown"


def test_unknown_schema_is_rejected_rather_than_guessed():
    with pytest.raises(ValueError, match="未知的官方欄位組合"):
        normalize_payload(payload("103年12月31日 變更交易", ["怪欄位"], []), raw_sha256="X")


def test_early_era_leaves_periodic_call_auction_unknown_not_false():
    """SPEC §2.2.5：缺資料不等於 0。早期不揭露分盤，補 False 會誤述成『沒有分盤』。"""
    frame = normalize_payload(
        payload("094年01月03日 全額交割證券", EARLY, [["1212", "中日"], ["1408", "中紡"]]),
        raw_sha256="ABC",
    )
    assert len(frame) == 2
    assert frame["periodic_call_auction"].isna().all()
    assert frame["altered_trading"].all()
    assert frame["source_schema"].eq("code_name_only").all()


def test_late_era_reads_the_double_star_mark():
    frame = normalize_payload(
        payload("096年12月31日 變更交易", LATE,
                [["1432", "大魯閣", "  "], ["1438", "裕豐", "**"]]),
        raw_sha256="ABC",
    )
    assert frame.set_index("stock_id")["periodic_call_auction"]["1438"]
    assert not frame.set_index("stock_id")["periodic_call_auction"]["1432"]


def test_failed_official_query_is_not_treated_as_an_empty_day():
    """stat 非 OK 代表查詢失敗，與『當日無變更交易』語意完全不同。"""
    with pytest.raises(ValueError, match="不得視為當日無事件"):
        normalize_payload(payload("x", LATE, [], stat="很抱歉，沒有符合條件的資料!"),
                          raw_sha256="ABC")


def test_placeholder_rows_are_dropped():
    """官方在無資料日會回一列說明文字，那不是證券。"""
    frame = normalize_payload(
        payload("103年12月31日 變更交易", LATE, [["本日無", "本日無", "  "]]),
        raw_sha256="ABC",
    )
    assert frame.empty


def test_empty_day_yields_no_rows_but_is_still_valid():
    frame = normalize_payload(payload("103年12月31日 變更交易", LATE, []), raw_sha256="ABC")
    assert frame.empty
    assert list(frame.columns)[:2] == ["snapshot_date", "stock_id"]


def test_restriction_frame_marks_only_observed_day_and_stock():
    obs = pd.DataFrame({
        "snapshot_date": [pd.Timestamp("2006-01-04")],
        "stock_id": ["1438"],
    })
    sessions = pd.DatetimeIndex(["2006-01-03", "2006-01-04", "2006-01-05"])
    frame = restriction_frame(obs, sessions, ["1438", "2330"])
    assert list(frame["1438"]) == [False, True, False]
    assert not frame["2330"].any()


def test_restriction_frame_ignores_stocks_outside_the_panel():
    obs = pd.DataFrame({
        "snapshot_date": [pd.Timestamp("2006-01-04")],
        "stock_id": ["9999"],
    })
    frame = restriction_frame(obs, pd.DatetimeIndex(["2006-01-04"]), ["1438"])
    assert not frame.to_numpy().any()
