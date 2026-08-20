"""影子模式在 daily_runner 的接線：空頭日必須「量測到、但不下單」。

tests/test_shadow_mode.py 只驗了旗標有沒有傳進 llm_ab_tracking。真正的風險
不在那裡，而在 daily_runner——影子日若不小心存了推薦或掛了單，使用者會看到
一份長得跟真的一樣的買進清單，而那天正是風控判定不該進場的日子。

所以這組測試直接跑 run_daily_recommendation()，把外部相依全部換成假物件，
逐一釘住三道保險：不掛單、不存推薦、不進報告。
"""
import datetime as dt

import pandas as pd
import pytest

from agent import daily_runner


class _Broker:
    """對齊 agent/broker.py 的 Broker 介面（sync / submit_exits / submit_entries
    / submit_reentries），只記錄有沒有被呼叫。"""

    name = "fake"

    def __init__(self):
        self.entries = []
        self.reentries = []

    def sync(self, on_date):
        return {"entries": [], "exits": [], "cancelled_exits": []}

    def submit_exits(self, on_date):
        return []

    def submit_entries(self, picks, on_date):
        self.entries.append(picks)
        return []

    def submit_reentries(self, candidates, on_date):
        self.reentries.append(candidates)
        return []


@pytest.fixture
def rig(monkeypatch):
    """把 run_daily_recommendation 的外部相依全部換掉，只留控制流程。"""
    state = {"saved": [], "ab_calls": [], "broker": _Broker()}

    candidates = pd.DataFrame([
        {"stock_id": "2330", "stock_name": "台積電", "industry": "半導體",
         "close": 900.0, "score": 9.1},
        {"stock_id": "2454", "stock_name": "聯發科", "industry": "半導體",
         "close": 1200.0, "score": 8.4},
    ])
    llm_result = {"recommendations": [{"stock_id": "2330", "reason": "營收年增"}],
                  "market_summary": "測試"}

    monkeypatch.setattr(daily_runner, "_latest_trade_date", lambda: dt.date(2026, 8, 10))
    monkeypatch.setattr(daily_runner, "run_sector_momentum", lambda *a, **k: None)
    monkeypatch.setattr(daily_runner, "get_broker", lambda: state["broker"])
    monkeypatch.setattr(daily_runner, "get_hot_sectors", lambda **k: ["半導體"])
    monkeypatch.setattr(daily_runner, "get_candidate_stocks", lambda *a, **k: candidates)
    monkeypatch.setattr(daily_runner, "format_candidates_for_llm", lambda c: "候選文字")
    monkeypatch.setattr(daily_runner, "generate_recommendations",
                        lambda *a, **k: dict(llm_result))
    monkeypatch.setattr(daily_runner, "save_recommendations",
                        lambda r: state["saved"].append(r))
    monkeypatch.setattr(daily_runner, "format_report", lambda r: "報告內容")
    monkeypatch.setattr(daily_runner, "format_positions_report", lambda *a, **k: "")

    # 出場檢查與部位查詢都不是本組測試的重點，一律回空
    monkeypatch.setattr(daily_runner, "get_session", lambda: _NullSession())

    def _record(signal_date, cands, result, pick_top_n=5, shadow=False):
        state["ab_calls"].append({"date": signal_date, "shadow": shadow,
                                  "n_quant": len(cands), "result": result})
        return {"quant_only": len(cands), "llm": 1, "written": True, "error": None}

    import agent.llm_ab_tracking as ab
    monkeypatch.setattr(ab, "record_daily_picks", _record)
    return state


class _NullSession:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a, **k): return _NullResult()


class _NullResult:
    def scalar(self): return dt.date(2026, 8, 10)
    def fetchall(self): return []
    def mappings(self): return self
    def all(self): return []


def _force_regime(monkeypatch, bull: bool):
    """市場濾網用的 regime 偵測是在函式內 import 的，直接改來源模組。"""
    import agent.stock_selector as sel
    monkeypatch.setattr(sel, "market_regime_detail",
                        lambda *a, **k: {"bull": bull, "state": "risk_on" if bull else "risk_off",
                                         "exposure_scale": 1.0})


def test_bear_day_records_ab_but_places_no_orders(rig, monkeypatch):
    _force_regime(monkeypatch, bull=False)

    daily_runner.run_daily_recommendation(with_entries=True)

    # 有量測到
    assert len(rig["ab_calls"]) == 1, "空頭日應該仍要記錄前向 A/B"
    assert rig["ab_calls"][0]["shadow"] is True
    # 但三道保險都要成立
    assert rig["broker"].entries == [], "影子日不得掛任何買單"
    assert rig["saved"] == [], "影子日不得存成正式推薦"


def test_bull_day_behaves_exactly_as_before(rig, monkeypatch):
    _force_regime(monkeypatch, bull=True)

    daily_runner.run_daily_recommendation(with_entries=True)

    assert len(rig["ab_calls"]) == 1
    assert rig["ab_calls"][0]["shadow"] is False
    assert rig["broker"].entries, "多頭日必須照常掛單"
    assert rig["saved"], "多頭日必須照常存推薦"


def test_weekend_mode_still_skips_everything(rig, monkeypatch):
    """週末不是影子日——完全不跑選股，才不會白花 LLM 額度。"""
    _force_regime(monkeypatch, bull=True)

    daily_runner.run_daily_recommendation(with_entries=False)

    assert rig["ab_calls"] == [], "週末模式不應該呼叫 LLM 或記錄 A/B"
    assert rig["broker"].entries == []
    assert rig["saved"] == []


def test_shadow_result_does_not_reach_the_report(rig, monkeypatch):
    """影子結果若流進報告，Telegram 會推出一份看起來像真的買進清單。"""
    _force_regime(monkeypatch, bull=False)

    out = daily_runner.run_daily_recommendation(with_entries=True)

    assert "recommendations" not in out or not out.get("recommendations"), \
        "影子日的推薦不得出現在回傳結果中"
