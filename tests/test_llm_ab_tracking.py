"""
LLM平行A/B量測測試（agent/llm_ab_tracking.py，SPEC_QUANT_UPGRADE.md決策點3）。
純函式部分（build_quant_only_rows/build_llm_rows）不需要DB；record_daily_picks
本身失敗容忍的行為用monkeypatch驗證，不打真實DB。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from agent.llm_ab_tracking import build_quant_only_rows, build_llm_rows, record_daily_picks


# ── build_quant_only_rows ────────────────────────────────────────
def test_build_quant_only_rows_takes_top_n_in_score_order():
    df = pd.DataFrame({"stock_id": ["A", "B", "C"], "score": [5.0, 9.0, 7.0]})
    rows = build_quant_only_rows(df, pick_top_n=2)
    # candidates 假設已依 score 排序（真實呼叫端 stock_selector 已排序過），
    # 這裡驗證的是「取前N筆、rank從1開始」的行為，不是自己重新排序
    assert [r["stock_id"] for r in rows] == ["A", "B"]
    assert [r["rank"] for r in rows] == [1, 2]
    assert rows[0]["score"] == 5.0


def test_build_quant_only_rows_empty_df_returns_empty():
    assert build_quant_only_rows(pd.DataFrame(), pick_top_n=5) == []


def test_build_quant_only_rows_none_returns_empty():
    assert build_quant_only_rows(None, pick_top_n=5) == []


def test_build_quant_only_rows_missing_score_column_returns_empty():
    df = pd.DataFrame({"stock_id": ["A"]})
    assert build_quant_only_rows(df, pick_top_n=5) == []


# ── build_llm_rows ────────────────────────────────────────────────
def test_build_llm_rows_extracts_stock_id_and_reason():
    result = {"recommendations": [
        {"stock_id": "2330", "reason": "投信連買"},
        {"stock_id": "2454", "reason": "月營收年增"},
    ]}
    rows = build_llm_rows(result)
    assert [r["stock_id"] for r in rows] == ["2330", "2454"]
    assert [r["rank"] for r in rows] == [1, 2]
    assert rows[0]["reason"] == "投信連買"


def test_build_llm_rows_none_result_returns_empty():
    assert build_llm_rows(None) == []


def test_build_llm_rows_no_recommendations_key_returns_empty():
    assert build_llm_rows({}) == []


def test_build_llm_rows_skips_entries_without_stock_id():
    result = {"recommendations": [{"reason": "沒有stock_id"}, {"stock_id": "2330", "reason": "ok"}]}
    rows = build_llm_rows(result)
    assert len(rows) == 1 and rows[0]["stock_id"] == "2330"


# ── record_daily_picks：失敗容忍，不拋例外 ──────────────────────────
def test_record_daily_picks_empty_inputs_is_noop(monkeypatch):
    called = {"ensure": False}
    monkeypatch.setattr("agent.llm_ab_tracking.ensure_llm_ab_tracking_table",
                        lambda: called.__setitem__("ensure", True))
    result = record_daily_picks(date(2026, 7, 20), pd.DataFrame(), None, pick_top_n=5)
    assert result == {"quant_only": 0, "llm": 0}
    assert called["ensure"] is False   # 兩組都空，連建表都不用做


def test_record_daily_picks_db_failure_does_not_raise(monkeypatch):
    def boom():
        raise RuntimeError("DB連不上")
    monkeypatch.setattr("agent.llm_ab_tracking.ensure_llm_ab_tracking_table", boom)
    df = pd.DataFrame({"stock_id": ["A"], "score": [1.0]})
    # 不應該拋例外——記錄失敗不能打斷正式推薦流程
    result = record_daily_picks(date(2026, 7, 20), df, None, pick_top_n=5)
    assert result["quant_only"] == 1   # 回傳的是「打算寫入的筆數」，不是「實際寫入成功的筆數」
