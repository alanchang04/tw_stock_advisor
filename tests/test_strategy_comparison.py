"""agent/strategy_comparison.py 的測試：重點在「不製造假可比性、不洩漏被抑制的數字」。"""
from __future__ import annotations

import json

import pytest

from agent import strategy_comparison as sc
from agent.strategy_comparison import (StrategyRow, comparability_warnings,
                                       load_comparison)


def test_loads_from_canonical_file():
    rows, meta, provisional = load_comparison()
    assert rows, "至少要載到現行策略"
    assert provisional is False, "已有 strategy_comparison_metrics.json，不該退回 adapter"
    assert meta.get("purpose"), "meta 要帶出權威檔的 purpose"
    assert {"current_swing", "mom1_b"} <= {r.key for r in rows}


def test_every_row_states_a_verdict():
    """沒有判決的策略不得出現在比較頁——那會讓它看起來像可選項。"""
    rows, _, _ = load_comparison()
    for r in rows:
        assert r.status, f"{r.key} 沒有 status"
        assert r.status_label


def test_source_report_present_except_unexecuted():
    rows, _, _ = load_comparison()
    for r in rows:
        if r.status != "not_executed":
            assert r.source_report, f"{r.key} 沒有 source_report"


def test_suppressed_metrics_are_not_promoted_into_main_fields():
    """樣本不足時，被抑制的 Sharpe/MDD/年化不得回到主要欄位。"""
    rows, _, _ = load_comparison()
    for r in rows:
        if r.suppressed:
            assert r.sharpe is None, f"{r.key} 的 Sharpe 應維持抑制"
            assert r.mdd is None
            assert r.ann_ret is None
            # 但原始值要保留可稽核
            assert "sharpe" in r.suppressed


def test_rejected_strategy_has_no_curve_dir():
    """一次性 holdout 不得產出或引用權益曲線。"""
    rows, _, _ = load_comparison()
    for r in rows:
        if r.status == "rejected_at_F1":
            assert not r.curve_dir, f"{r.key} 不該有 curve_dir"
            assert r.has_curve is False


def test_mom1_exposes_annual_returns_only():
    rows, _, _ = load_comparison()
    mom1 = next((r for r in rows if r.key == "mom1_b"), None)
    assert mom1 is not None
    assert len(mom1.annual_returns) == 7, "2008~2014 共 7 個年度"
    assert mom1.has_curve is False


def test_sample_text_never_merges_different_units():
    row = StrategyRow(key="x", display_name="X", status="sample_insufficient",
                      trades=6, observations=82, calendar_years=0.33)
    text = row.sample_text
    assert "6 筆交易" in text and "82 個觀測" in text and "0.33 年" in text


def test_sample_adequacy_prefers_episodes_over_trades():
    """反轉類策略的樣本單位是情境不是交易；有情境數就以它為準。"""
    assert StrategyRow(key="a", display_name="A", status="x",
                       trades=200, independent_episodes=12).sample_is_adequate is False
    assert StrategyRow(key="b", display_name="B", status="x",
                       trades=10).sample_is_adequate is False
    assert StrategyRow(key="c", display_name="C", status="x",
                       trades=475).sample_is_adequate is True
    assert StrategyRow(key="d", display_name="D", status="x").sample_is_adequate is None


def test_rejected_warning_forbids_hypothesis_generation_from_holdout():
    rows = [StrategyRow(key="m", display_name="MOM-1B", status="rejected_at_F1")]
    text = " ".join(comparability_warnings(rows))
    assert "污染" in text, "必須講明用 holdout 探索即為污染，不只是不得調參"


def test_suppressed_rows_trigger_a_warning():
    rows = [StrategyRow(key="s", display_name="融資反轉",
                        status="sample_insufficient",
                        suppressed={"sharpe": -3.19})]
    text = " ".join(comparability_warnings(rows))
    assert "抑制" in text and "融資反轉" in text


def test_unknown_status_falls_back_to_raw_string():
    assert StrategyRow(key="z", display_name="Z",
                       status="some_future_status").status_label == "some_future_status"


def test_missing_canonical_falls_back_and_flags_provisional(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CANONICAL", tmp_path / "does_not_exist.json")
    rows, meta, provisional = load_comparison()
    assert provisional is True
    assert meta == {}


def test_no_ranking_or_composite_score_is_exposed():
    """本模組刻意不提供排名/評分——期間與樣本單位不同，排名是假可比。"""
    for banned in ("rank", "score", "winner"):
        assert not any(banned in name.lower()
                       for name in dir(sc) if not name.startswith("_"))


def test_canonical_unknown_fields_do_not_crash(tmp_path, monkeypatch):
    """研究機日後加欄位不得讓頁面爆掉，但也不能被靜默當成已知欄位。"""
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps({
        "schema_version": 99,
        "strategies": [{"key": "k", "display_name": "K", "status": "not_executed",
                        "brand_new_field": 123}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sc, "CANONICAL", path)
    rows, _, provisional = load_comparison()
    assert provisional is False
    assert rows[0].key == "k"
    assert not hasattr(rows[0], "brand_new_field")
