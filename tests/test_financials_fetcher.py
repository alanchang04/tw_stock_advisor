"""
季度財報擷取測試（data_pipeline/fetchers/financials_fetcher.py，SPEC_STRATEGY_MIDCAP 決策5）。
純函式（_num/_clamp_pct/_get）+ 合併邏輯（monkeypatch _fetch_json 不打網路）+ DB upsert
（monkeypatch get_session 換成不 commit 的共用交易，同 test_stock_analysis.py 手法）。
"""
import os
import sys
from contextlib import contextmanager

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_pipeline.fetchers.financials_fetcher as ff


def test_num_parses_and_handles_blanks():
    assert ff._num("1,234.50") == 1234.50
    assert ff._num("") is None
    assert ff._num("-") is None
    assert ff._num(None) is None


def test_clamp_pct_drops_out_of_range():
    assert ff._clamp_pct(50.0) == 50.0
    assert ff._clamp_pct(99999.0) is None
    assert ff._clamp_pct(-99999.0) is None
    assert ff._clamp_pct(None) is None


def test_get_tries_keys_in_order():
    row = {"SecuritiesCompanyCode": "1240", "公司代號": ""}
    assert ff._get(row, "公司代號", "SecuritiesCompanyCode") == "1240"
    assert ff._get(row, "missing_key") is None


def _fake_income_twse():
    return [{
        "年度": "114", "季別": "4", "公司代號": "TESTFIN01",
        "營業收入": "1000000.00", "營業毛利（毛損）淨額": "300000.00",
        "營業利益（損失）": "150000.00",
        "淨利（淨損）歸屬於母公司業主": "100000.00",
        "基本每股盈餘（元）": "2.50",
    }]


def _fake_balance_twse():
    return [{
        "公司代號": "TESTFIN01",
        "資產總額": "5000000.00", "負債總額": "2000000.00",
        "歸屬於母公司業主之權益合計": "3000000.00",
    }]


def test_fetch_financials_merges_income_and_balance(monkeypatch):
    def fake_fetch_json(url):
        if "t187ap06" in url:
            return _fake_income_twse()
        if "t187ap07" in url:
            return _fake_balance_twse()
        return []
    monkeypatch.setattr(ff, "_fetch_json", fake_fetch_json)

    captured = {}

    @contextmanager
    def fake_session():
        class FakeSession:
            def execute(self, stmt, params=None):
                sql = str(stmt)
                if "SELECT stock_id FROM stocks" in sql:
                    class R:
                        def fetchall(self_r):
                            return [("TESTFIN01",)]
                    return R()
                captured["rows"] = params
                class R2:
                    def fetchall(self_r): return []
                return R2()
        yield FakeSession()
    monkeypatch.setattr(ff, "get_session", fake_session)

    n = ff.fetch_financials()
    assert n == 1
    row = captured["rows"][0]
    assert row["sid"] == "TESTFIN01"
    assert row["revenue"] == 1_000_000              # MOPS 原始值已是千元，不再乘 1000
    assert row["gross_margin"] == pytest.approx(30.0)
    assert row["operating_margin"] == pytest.approx(15.0)
    assert row["debt_ratio"] == pytest.approx(40.0)
    assert row["roe"] == pytest.approx(100000 / 3000000 * 100)
    assert row["roa"] == pytest.approx(100000 / 5000000 * 100)
    assert row["eps"] == 2.50


def test_fetch_financials_skips_unknown_stock_ids(monkeypatch):
    monkeypatch.setattr(ff, "_fetch_json", lambda url:
        _fake_income_twse() if "t187ap06" in url else _fake_balance_twse())

    @contextmanager
    def fake_session_no_match():
        class FakeSession:
            def execute(self, stmt, params=None):
                sql = str(stmt)
                if "SELECT stock_id FROM stocks" in sql:
                    class R:
                        def fetchall(self_r): return []   # TESTFIN01 不存在
                    return R()
                pytest.fail("不該執行到 INSERT（全部股票都被過濾掉）")
        yield FakeSession()
    monkeypatch.setattr(ff, "get_session", fake_session_no_match)

    n = ff.fetch_financials()
    assert n == 0


# ── DB 整合：真實 schema，交易內回滾，不寫入正式資料 ────────────────
@pytest.fixture
def tx(monkeypatch):
    try:
        from database.connection import get_session_factory
        session = get_session_factory()()
        session.execute(text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"DB 無法連線，跳過整合測試：{e}")

    @contextmanager
    def _shared():
        yield session

    monkeypatch.setattr(ff, "get_session", _shared)
    yield session
    session.rollback()
    session.close()


def test_fetch_financials_upserts_real_schema(monkeypatch, tx):
    sid = "TESTFIN02"
    tx.execute(text("INSERT INTO stocks (stock_id, stock_name, market) VALUES (:s, 'test', 'TWSE') ON CONFLICT DO NOTHING"),
              {"s": sid})
    tx.execute(text("DELETE FROM financials WHERE stock_id = :s"), {"s": sid})

    def fake_fetch_json(url):
        if "t187ap06" in url:
            return [{**_fake_income_twse()[0], "公司代號": sid}]
        if "t187ap07" in url:
            return [{**_fake_balance_twse()[0], "公司代號": sid}]
        return []
    monkeypatch.setattr(ff, "_fetch_json", fake_fetch_json)

    n = ff.fetch_financials()
    assert n == 1
    row = tx.execute(text("SELECT eps, roe, gross_margin FROM financials WHERE stock_id = :s"),
                     {"s": sid}).fetchone()
    assert row is not None
    assert float(row[0]) == 2.50
