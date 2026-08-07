"""record_daily_picks 必須回報「有沒有真的寫進去」。

2026-08-07 盤點發現：factor_screen 跑了 4 天但前向 A/B 只有 3 天，差的那天
無從追查——因為失敗只寫一行 logger.warning。而這條前向樣本每 5 個 pipeline 日
才產出 1 個訊號日，掉一天等於掉 1/30 的最終樣本，不能讓它無聲失敗。
"""
import datetime as dt

import pandas as pd

from agent import llm_ab_tracking


def _candidates() -> pd.DataFrame:
    return pd.DataFrame([
        {"stock_id": "2330", "score": 9.1},
        {"stock_id": "2454", "score": 8.4},
    ])


def _result() -> dict:
    return {"recommendations": [{"stock_id": "2330", "reason": "營收年增強勁"}]}


def test_success_reports_written_true(monkeypatch):
    monkeypatch.setattr(llm_ab_tracking, "ensure_llm_ab_tracking_table", lambda: None)

    class _S:
        def execute(self, *a, **k):
            return None

    class _Ctx:
        def __enter__(self): return _S()
        def __exit__(self, *a): return False

    monkeypatch.setattr(llm_ab_tracking, "get_session", lambda: _Ctx())

    out = llm_ab_tracking.record_daily_picks(
        dt.date(2026, 8, 7), _candidates(), _result(), pick_top_n=2)

    assert out["written"] is True
    assert out["error"] is None
    assert out["quant_only"] == 2
    assert out["llm"] == 1


def test_db_failure_reports_written_false_with_reason(monkeypatch):
    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(llm_ab_tracking, "ensure_llm_ab_tracking_table", _boom)

    out = llm_ab_tracking.record_daily_picks(
        dt.date(2026, 8, 7), _candidates(), _result(), pick_top_n=2)

    # 不可拋例外——輔助功能不能打斷正式推薦流程
    assert out["written"] is False
    assert "RuntimeError" in out["error"]
    assert "connection refused" in out["error"]


def test_empty_inputs_are_reported_as_not_written():
    out = llm_ab_tracking.record_daily_picks(
        dt.date(2026, 8, 7), pd.DataFrame(), None, pick_top_n=5)

    assert out["written"] is False
    assert out["error"]
    assert out["quant_only"] == 0 and out["llm"] == 0
