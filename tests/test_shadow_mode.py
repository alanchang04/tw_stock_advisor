"""影子模式：空頭日照常選股＋辯論並記錄前向 A/B，但絕不掛單、絕不進報告。

2026-08-07 盤點：15 個 pipeline 日有 7 天被市場濾網擋掉，前向 A/B 只累積到
3 個訊號日。而且缺口不是隨機的——濾網專擋空頭，樣本因此結構性偏向多頭。

影子模式把「測量」與「下單」拆開。**最重要的性質是它不能改變任何交易行為**，
所以這組測試主要在釘住「不會下單、不會被存成推薦、不會出現在報告」。
"""
import datetime as dt

import pandas as pd

from agent import llm_ab_tracking


def _candidates() -> pd.DataFrame:
    return pd.DataFrame([{"stock_id": "2330", "score": 9.1},
                         {"stock_id": "2454", "score": 8.4}])


def _result() -> dict:
    return {"recommendations": [{"stock_id": "2330", "reason": "營收年增"}]}


class _Recorder:
    """收集寫入參數，驗證 shadow 旗標有真的傳到 SQL。"""

    def __init__(self):
        self.params = []

    def execute(self, _stmt, params=None):
        if params:
            self.params.append(params)
        return None


def _patch_session(monkeypatch, recorder):
    class _Ctx:
        def __enter__(self): return recorder
        def __exit__(self, *a): return False

    monkeypatch.setattr(llm_ab_tracking, "ensure_llm_ab_tracking_table", lambda: None)
    monkeypatch.setattr(llm_ab_tracking, "get_session", lambda: _Ctx())


def test_shadow_flag_is_written_to_every_row(monkeypatch):
    rec = _Recorder()
    _patch_session(monkeypatch, rec)

    out = llm_ab_tracking.record_daily_picks(
        dt.date(2026, 8, 8), _candidates(), _result(), pick_top_n=2, shadow=True)

    assert out["written"] is True
    assert rec.params, "沒有任何寫入"
    assert all(p["shadow"] is True for p in rec.params)


def test_normal_day_records_shadow_false(monkeypatch):
    rec = _Recorder()
    _patch_session(monkeypatch, rec)

    llm_ab_tracking.record_daily_picks(
        dt.date(2026, 8, 8), _candidates(), _result(), pick_top_n=2)

    assert all(p["shadow"] is False for p in rec.params)


def test_shadow_defaults_to_false_so_existing_callers_are_unchanged(monkeypatch):
    """既有呼叫端沒傳 shadow 時，語意必須維持「真實交易日」。"""
    rec = _Recorder()
    _patch_session(monkeypatch, rec)

    llm_ab_tracking.record_daily_picks(
        dt.date(2026, 8, 8), _candidates(), _result())

    assert rec.params and all(p["shadow"] is False for p in rec.params)


def test_shadow_and_live_rows_are_distinguishable(monkeypatch):
    """同一份選股在影子日與真實日必須寫出不同的 shadow 值——分析時要分得出來，
    否則會把「假設會買」誤讀成「真的買了」。"""
    rec = _Recorder()
    _patch_session(monkeypatch, rec)

    llm_ab_tracking.record_daily_picks(
        dt.date(2026, 8, 8), _candidates(), _result(), pick_top_n=2, shadow=True)
    n_shadow = len(rec.params)
    llm_ab_tracking.record_daily_picks(
        dt.date(2026, 8, 9), _candidates(), _result(), pick_top_n=2, shadow=False)

    assert all(p["shadow"] is True for p in rec.params[:n_shadow])
    assert all(p["shadow"] is False for p in rec.params[n_shadow:])
