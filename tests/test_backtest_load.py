"""
agent/backtest.py `_load()` 的 SQL 查詢測試（不需真實 DB，monkeypatch get_session）。

2026-07-20背景：`_load()`（直連 Neon 分支）原本對 daily_prices/technical_indicators/
institutional_trading 三張表做無 WHERE、無 LIMIT 的全表查詢。`run_pipeline.py`
`_weekly_review_text()` 每週五排程呼叫，全表查詢把 Neon 免費層的網路傳輸配額燒穿
（詳見 docs/SPEC_QUANT_UPGRADE.md §2.8）。這裡鎖住修正後的行為：不帶 since 維持
原本全表查詢（跨週期研究/回測需要），帶 since 才會加上日期篩選大幅縮小傳輸量。
"""
import os
import sys
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.backtest as bt


class _FakeResult:
    def fetchall(self):
        return []


class _FakeSession:
    def __init__(self):
        self.calls = []   # list of (sql_text, params_dict)

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params or {}))
        return _FakeResult()


class _FakeSessionCtx:
    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *a):
        return False


def test_load_without_since_queries_full_table(monkeypatch):
    fake = _FakeSession()
    monkeypatch.setattr(bt, "get_session", lambda: _FakeSessionCtx(fake))
    bt._load()
    prices_sql, prices_params = fake.calls[0]
    assert "WHERE" not in prices_sql
    assert prices_params == {}


def test_load_with_since_filters_the_three_large_tables(monkeypatch):
    fake = _FakeSession()
    monkeypatch.setattr(bt, "get_session", lambda: _FakeSessionCtx(fake))
    since = dt.date(2026, 1, 1)
    bt._load(since=since)
    # 前三個呼叫依序是 daily_prices / technical_indicators / institutional_trading
    for sql, params in fake.calls[:3]:
        assert "WHERE trade_date >= :since" in sql
        assert params == {"since": since}


def test_load_with_since_leaves_small_reference_tables_unfiltered(monkeypatch):
    fake = _FakeSession()
    monkeypatch.setattr(bt, "get_session", lambda: _FakeSessionCtx(fake))
    bt._load(since=dt.date(2026, 1, 1))
    # 第4個呼叫是 stock_industry_map（小型參考表，不受 since 影響）
    imap_sql, imap_params = fake.calls[3]
    assert "WHERE" not in imap_sql
    assert imap_params == {}


def test_run_backtest_passes_buffered_since_when_start_date_given(monkeypatch):
    """run_backtest(start_date=X) 走 Neon 分支時，應該把 since=X-120天 往下傳給
    _load()，而不是查全表——這是本次修正的關鍵串接，漏了這段 since 就永遠是 None。"""
    captured = {}

    import pandas as pd

    def fake_load(parquet_dir=None, since=None):
        captured["parquet_dir"] = parquet_dir
        captured["since"] = since
        # 帶一列假資料而不是完全空，讓 run_backtest 走到「區間太短」的正常早退，
        # 而不是在 min()/max() 這種空欄位運算就先炸掉（跟這個測試要驗的東西無關）。
        one_row_date = [dt.date(2020, 1, 2)]
        return {
            "prices": pd.DataFrame({"stock_id": ["2330"], "trade_date": one_row_date,
                                    "open": [100.0], "close": [100.0], "volume": [1000.0],
                                    "turnover": [1e8], "change_pct": [0.0]}),
            "tech": pd.DataFrame(columns=["stock_id", "trade_date", "ma5", "ma20", "ma60",
                                          "rsi14", "macd_hist", "signal_ma_cross", "signal_breakout"]),
            "inst": pd.DataFrame({"stock_id": ["2330"], "trade_date": one_row_date,
                                  "total_net": [0.0], "foreign_net": [0.0], "invest_net": [0.0]}),
            "imap": pd.DataFrame(columns=["stock_id", "industry_code"]),
            "inds": pd.DataFrame(columns=["industry_code", "name_zh"]),
            "rev_map": {}, "dividends": pd.DataFrame(columns=["stock_id", "ex_date", "pre_close", "ref_price"]),
        }

    monkeypatch.setattr(bt, "_load", fake_load)
    start = dt.date(2026, 1, 1)
    bt.run_backtest(start_date=start, quiet=True)
    assert captured["since"] == start - dt.timedelta(days=120)
