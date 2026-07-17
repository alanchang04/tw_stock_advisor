"""
除權息事件/下市清單抓取測試（data_pipeline/fetchers/corporate_actions_fetcher.py，
SPEC_QUANT_UPGRADE.md P0-2）。只測純函式（不打網路）：日期解析、數字清理、
TWSE/TPEX 欄位對照解析。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_pipeline.fetchers.corporate_actions_fetcher import (
    _roc_to_date, _num, _parse_roc_slash, fetch_dividend_events_twse,
    fetch_dividend_events_tpex, _EVENT_TYPE_MAP,
)


# ── 日期解析 ─────────────────────────────────────────────────────
def test_roc_to_date_with_chinese_chars():
    assert _roc_to_date("113年07月01日") == date(2024, 7, 1)

def test_roc_to_date_with_slashes():
    assert _roc_to_date("113/07/01") == date(2024, 7, 1)

def test_roc_to_date_invalid_returns_none():
    assert _roc_to_date("not a date") is None
    assert _roc_to_date("") is None

def test_parse_roc_slash():
    assert _parse_roc_slash("115/06/23") == date(2026, 6, 23)
    assert _parse_roc_slash("") is None
    assert _parse_roc_slash(None) is None
    assert _parse_roc_slash("garbage") is None


# ── 數字清理 ─────────────────────────────────────────────────────
def test_num_parses_and_handles_blanks():
    assert _num("34.20") == 34.20
    assert _num("1,234.5") == 1234.5
    assert _num("") is None
    assert _num("-") is None
    assert _num(None) is None


# ── event type 對照 ──────────────────────────────────────────────
def test_event_type_map_covers_both_market_conventions():
    assert _EVENT_TYPE_MAP["息"] == "除息"
    assert _EVENT_TYPE_MAP["權"] == "除權"
    assert _EVENT_TYPE_MAP["除權息"] == "除權息"


# ── TWSE JSON 解析（monkeypatch requests，不打網路）───────────────
class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self): pass
    def json(self): return self._payload


def test_fetch_dividend_events_twse_parses_rows(monkeypatch):
    payload = {
        "stat": "OK",
        "fields": ["資料日期", "股票代號", "股票名稱", "除權息前收盤價", "除權息參考價",
                   "權值+息值", "權/息"],
        "data": [
            ["113年07月01日", "1101", "台泥", "34.20", "33.20", "1.000000", "息"],
            ["113年07月01日", "1101B", "台泥乙特", "48.40", "46.63", "1.763623", "息"],  # 特別股代號跳過
        ],
    }
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(payload))
    df = fetch_dividend_events_twse(date(2024, 7, 1), date(2024, 7, 31))
    assert len(df) == 1   # 1101B（非4碼純數字）被排除
    row = df.iloc[0]
    assert row["stock_id"] == "1101"
    assert row["ex_date"] == date(2024, 7, 1)
    assert row["pre_close"] == 34.20
    assert row["ref_price"] == 33.20
    assert row["cash_dividend"] == pytest_approx(1.0)
    assert row["event_type"] == "除息"
    assert row["market"] == "TWSE"


def test_fetch_dividend_events_twse_empty_on_bad_stat(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp({"stat": "ERROR", "data": []}))
    df = fetch_dividend_events_twse(date(2024, 7, 1), date(2024, 7, 31))
    assert df.empty


def test_fetch_dividend_events_twse_network_error_returns_empty(monkeypatch):
    def _raise(*a, **k): raise ConnectionError("boom")
    monkeypatch.setattr("requests.get", _raise)
    df = fetch_dividend_events_twse(date(2024, 7, 1), date(2024, 7, 31))
    assert df.empty


# ── TPEX JSON 解析 ────────────────────────────────────────────────
def test_fetch_dividend_events_tpex_parses_rows_with_split_cash_stock(monkeypatch):
    payload = {
        "stat": "ok",
        "tables": [{
            "fields": ["除權息日期", "代號", "名稱", "除權息前收盤價", "除權息參考價",
                      "權值", "息值", "權值+息值", "權/息", "漲停價"],
            "data": [
                ["113/07/01", "3373", "熱映", "27.00", "26.00", "0.000000", "1.000000", "1.000000", "除息", "28.60"],
                ["113/07/01", "4188", "廠", "27.6", "26.87", "0.726487", "0.0", "0.73", "除權", "29.5"],
            ],
        }],
    }
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(payload))
    df = fetch_dividend_events_tpex(date(2024, 7, 1), date(2024, 7, 31))
    assert len(df) == 2
    r1 = df[df["stock_id"] == "3373"].iloc[0]
    assert r1["cash_dividend"] == pytest_approx(1.0)
    assert r1["stock_dividend_ratio"] == pytest_approx(0.0)
    r2 = df[df["stock_id"] == "4188"].iloc[0]
    assert r2["stock_dividend_ratio"] == pytest_approx(0.726487)


def test_fetch_dividend_events_tpex_empty_tables_returns_empty(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp({"stat": "ok", "tables": []}))
    df = fetch_dividend_events_tpex(date(2024, 7, 1), date(2024, 7, 31))
    assert df.empty


def pytest_approx(v, rel=1e-6):
    import pytest
    return pytest.approx(v, rel=rel)
