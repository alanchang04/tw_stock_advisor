"""
月營收擷取測試（data_pipeline/fetchers/revenue_fetcher.py，SPEC_QUANT_UPGRADE.md P1
月營收因子缺口回補：本機10年回補要重用 fetch_month_rows 這個純抓取函式，
2026-07-19 從 fetch_month() 中拆出來時順手補測試，鎖住行為不被之後改動破壞）。
"""
import os
import sys
import datetime as _dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_pipeline.fetchers.revenue_fetcher import (
    _num, latest_published_month, fetch_month_rows,
)


# ── 數字清理 ─────────────────────────────────────────────────────
def test_num_parses_and_handles_blanks():
    assert _num("1,234") == 1234.0
    assert _num("32.39") == 32.39
    assert _num("-") is None
    assert _num("") is None
    assert _num("不適用") is None


# ── 最新已公布月份（每月10日為分界）───────────────────────────────
def test_latest_published_month_after_10th_is_last_month():
    assert latest_published_month(_dt.date(2026, 7, 15)) == (2026, 6)

def test_latest_published_month_on_or_before_10th_is_two_months_ago():
    assert latest_published_month(_dt.date(2026, 7, 10)) == (2026, 5)

def test_latest_published_month_crosses_year_boundary():
    assert latest_published_month(_dt.date(2026, 1, 5)) == (2025, 11)


# ── fetch_month_rows（monkeypatch requests，不打網路）─────────────
_SII_HTML = """
<table><tr>
<td>2330</td><td>台積電</td><td>13,382,706</td><td>x</td><td>x</td><td>6.11</td><td>32.39</td>
</tr></table>
"""
_EMPTY_HTML = "<table></table>"


class _FakeResp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code
        self.encoding = None
    def raise_for_status(self):
        pass


def test_fetch_month_rows_parses_sii_and_otc(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return _FakeResp(_SII_HTML if "/sii/" in url else _EMPTY_HTML)

    import data_pipeline.fetchers.revenue_fetcher as mod
    monkeypatch.setattr(mod.requests, "get", fake_get)

    rows = fetch_month_rows(2026, 6)
    assert len(rows) == 1
    assert rows[0] == {"sid": "2330", "ym": "2026-06", "rev": 13382706, "mom": 6.11, "yoy": 32.39}


def test_fetch_month_rows_http_error_skips_market_not_crashes(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return _FakeResp("", status_code=502)

    import data_pipeline.fetchers.revenue_fetcher as mod
    monkeypatch.setattr(mod.requests, "get", fake_get)

    assert fetch_month_rows(2026, 6) == []


def test_fetch_month_rows_request_exception_skips_market_not_crashes(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        raise ConnectionError("boom")

    import data_pipeline.fetchers.revenue_fetcher as mod
    monkeypatch.setattr(mod.requests, "get", fake_get)

    assert fetch_month_rows(2026, 6) == []
