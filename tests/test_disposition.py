"""處置股資料與排除邏輯（SPEC_QUANT_UPGRADE §2.4）。

解析用真實抓下來的欄位格式當樣本——民國年、全形波浪號、公布日期的 '*' 前綴
都是實測資料裡真的出現過的形態，不是想像出來的邊界。
"""
from datetime import date

import pandas as pd
import pytest

from agent.strategy import (STRATEGY, build_disposition_index, exclude_disposition,
                            is_under_disposition)
from data_pipeline.fetchers.disposition_fetcher import (parse_notice_rows, parse_punish_rows,
                                                        parse_roc_date, parse_roc_period)


# ── 民國日期解析 ──────────────────────────────────────────────────
@pytest.mark.parametrize("s,expected", [
    ("113/10/07", date(2024, 10, 7)),
    ("113.10.07", date(2024, 10, 7)),      # notice 端點用點分隔
    ("*115/07/24", date(2026, 7, 24)),     # 更正過的公告會前綴 *
    (" 115/07/24 ", date(2026, 7, 24)),
    ("99/01/05", date(2010, 1, 5)),        # 兩位數民國年
])
def test_parse_roc_date(s, expected):
    assert parse_roc_date(s) == expected


@pytest.mark.parametrize("s", [None, "", "2024-10-07", "abc", "113/13/45"])
def test_parse_roc_date_rejects_bad_input(s):
    assert parse_roc_date(s) is None


def test_parse_roc_period_fullwidth_tilde():
    assert parse_roc_period("113/10/07～113/10/21") == (date(2024, 10, 7), date(2024, 10, 21))


def test_parse_roc_period_halfwidth_tilde():
    assert parse_roc_period("113/10/07~113/10/21") == (date(2024, 10, 7), date(2024, 10, 21))


def test_parse_roc_period_bad_input():
    assert parse_roc_period("113/10/07") == (None, None)


# ── punish 解析 ──────────────────────────────────────────────────
def _punish_row(no, announce, sid, name, cum, cond, period, measure):
    return [no, announce, sid, name, cum, cond, period, measure, "處置內容…", ""]


def test_parse_punish_keeps_only_4digit_stocks():
    """權證/ETN 是 6 位數，佔原始資料六成以上，不在候選池裡就不該存。"""
    rows = [
        _punish_row(1, "113/08/19", "1225", "福懋油", 1, "最近十個營業日已有六次",
                    "113/08/20～113/09/02", "第一次處置"),
        _punish_row(2, "113/10/04", "068389", "T50反1元大3B購01", 1, "連續三次",
                    "113/10/07～113/10/21", "第一次處置"),
    ]
    df = parse_punish_rows(rows)
    assert list(df["stock_id"]) == ["1225"]
    assert df.iloc[0]["start_date"] == date(2024, 8, 20)
    assert df.iloc[0]["end_date"] == date(2024, 9, 2)
    assert df.iloc[0]["announce_date"] == date(2024, 8, 19)
    assert df.iloc[0]["cumulative"] == 1


def test_parse_punish_drops_rows_without_parsable_period():
    """起迄解不出來的列留著也沒用——這張表的用途就是判斷某天在不在處置期間。"""
    rows = [_punish_row(1, "113/08/19", "1225", "福懋油", 1, "x", "無資料", "第一次處置")]
    assert parse_punish_rows(rows).empty


def test_parse_punish_dedupes_corrections():
    """同一段處置期間可能因更正重覆公布，留最後一筆。"""
    rows = [
        _punish_row(1, "113/02/02", "1418", "東華", 1, "連續三次",
                    "113/02/05～113/02/27", "第一次處置"),
        _punish_row(1, "*113/02/03", "1418", "東華", 1, "連續三次",
                    "113/02/05～113/02/27", "第一次處置"),
    ]
    df = parse_punish_rows(rows)
    assert len(df) == 1
    assert df.iloc[0]["announce_date"] == date(2024, 2, 3)


def test_parse_punish_empty():
    assert parse_punish_rows([]).empty
    assert parse_punish_rows(None).empty


def test_parse_notice_rows():
    rows = [
        [1, "006207", "復華滬深", "1", "最近六個營業日累積收盤價漲幅達32.36%﹝第一款﹞。",
         "113.10.07", "30.46", "-----"],
        [2, "2330", "台積電", "1", "注意理由…", "113.10.08", "1000", "20"],
    ]
    df = parse_notice_rows(rows)
    assert list(df["stock_id"]) == ["2330"]      # 006207 是 6 位數 ETF 代號
    assert df.iloc[0]["notice_date"] == date(2024, 10, 8)


# ── 排除邏輯（live 與回測共用的純函式）────────────────────────────
@pytest.fixture
def idx():
    return build_disposition_index(pd.DataFrame([
        {"stock_id": "1225", "start_date": date(2024, 8, 20), "end_date": date(2024, 9, 2)},
        {"stock_id": "1225", "start_date": date(2024, 11, 1), "end_date": date(2024, 11, 14)},
        {"stock_id": "2434", "start_date": date(2026, 7, 20), "end_date": date(2026, 7, 31)},
    ]))


@pytest.mark.parametrize("sid,d,expected", [
    ("1225", date(2024, 8, 20), True),    # 起始日含在內
    ("1225", date(2024, 9, 2), True),     # 結束日含在內
    ("1225", date(2024, 8, 19), False),   # 前一天還沒開始
    ("1225", date(2024, 9, 3), False),    # 後一天已結束
    ("1225", date(2024, 11, 5), True),    # 同一檔的第二段處置
    ("1225", date(2024, 10, 1), False),   # 兩段之間
    ("2330", date(2024, 8, 20), False),   # 沒被處置過的股票
])
def test_is_under_disposition(sid, d, expected, idx):
    assert is_under_disposition(sid, d, idx) is expected


def test_empty_index_never_excludes():
    """資料還沒回補時整套機制必須自動停用，不能讓既有回測跑不動。"""
    assert is_under_disposition("1225", date(2024, 8, 20), {}) is False
    assert build_disposition_index(None) == {}
    assert build_disposition_index(pd.DataFrame()) == {}


def test_exclude_disposition_splits_candidates(idx):
    df = pd.DataFrame({"stock_id": ["1225", "2330", "2454"], "close": [50.0, 1000.0, 900.0]})
    keep, dropped = exclude_disposition(df, date(2024, 8, 21), idx)
    assert list(keep["stock_id"]) == ["2330", "2454"]
    assert list(dropped["stock_id"]) == ["1225"]


def test_exclude_disposition_outside_period_keeps_all(idx):
    df = pd.DataFrame({"stock_id": ["1225", "2330"], "close": [50.0, 1000.0]})
    keep, dropped = exclude_disposition(df, date(2024, 10, 1), idx)
    assert len(keep) == 2 and dropped.empty


def test_exclude_disposition_can_be_turned_off_for_ablation(idx):
    df = pd.DataFrame({"stock_id": ["1225"], "close": [50.0]})
    keep, dropped = exclude_disposition(df, date(2024, 8, 21), idx,
                                        cfg={**STRATEGY, "exclude_disposition": False})
    assert len(keep) == 1 and dropped.empty


def test_exclude_disposition_is_on_by_default():
    """這是寫實度修正不是選配優化——預設關掉就等於沒做。"""
    assert STRATEGY["exclude_disposition"] is True
