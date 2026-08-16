"""agent/strategy_comparison.py 的測試：重點在「不製造假可比性」。"""
from __future__ import annotations

import json

import pytest

from agent import strategy_comparison as sc
from agent.strategy_comparison import (StrategyRow, comparability_warnings,
                                       load_comparison)


def test_loads_rows_from_committed_reports():
    rows, provisional = load_comparison()
    assert rows, "至少要載到現行策略"
    assert isinstance(provisional, bool)
    keys = {r.key for r in rows}
    assert "current_swing" in keys


def test_every_row_carries_its_source_report():
    """每個數字都要指得回出處，否則對不上時無從追查。"""
    rows, _ = load_comparison()
    for r in rows:
        assert r.source_report, f"{r.key} 沒有 source_report"
        assert r.source_report.startswith("reports/")


def test_win_rate_always_states_its_basis():
    """月勝率與逐筆交易勝率不是同一件事，有數字就必須有基準。"""
    rows, _ = load_comparison()
    for r in rows:
        if r.win_rate is not None:
            assert r.win_rate_basis, f"{r.key} 有勝率卻沒說明基準"


def test_mixed_win_rate_bases_trigger_a_warning():
    rows = [
        StrategyRow(key="a", display_name="A", status="frozen_satellite",
                    win_rate=0.35, win_rate_basis="逐筆交易", sample_n=475),
        StrategyRow(key="b", display_name="B", status="rejected_at_F1",
                    win_rate=0.55, win_rate_basis="月", sample_n=83),
    ]
    warnings = " ".join(comparability_warnings(rows))
    assert "勝率定義不同" in warnings


def test_small_sample_is_called_out():
    rows = [StrategyRow(key="x", display_name="小樣本策略",
                        status="insufficient_sample", sample_n=6)]
    warnings = " ".join(comparability_warnings(rows))
    assert "樣本不足" in warnings
    assert "小樣本策略" in warnings


def test_rejected_strategy_warning_mentions_no_retuning():
    rows = [StrategyRow(key="m", display_name="MOM-1A", status="rejected_at_F1")]
    warnings = " ".join(comparability_warnings(rows))
    assert "否決" in warnings
    assert "調整參數" in warnings or "搶救" in warnings


def test_unknown_status_falls_back_to_raw_string():
    row = StrategyRow(key="z", display_name="Z", status="some_future_status")
    assert row.status_label == "some_future_status"


def test_missing_curve_dir_reports_false():
    row = StrategyRow(key="z", display_name="Z", status="not_executed",
                      curve_dir="reports/does_not_exist_xyz")
    assert row.has_curve is False
    assert StrategyRow(key="z", display_name="Z", status="not_executed").has_curve is False


def test_canonical_file_takes_precedence(tmp_path, monkeypatch):
    """研究機提供正規化檔之後，一律以它為準，且 provisional 轉為 False。"""
    canonical = tmp_path / "strategy_comparison_metrics.json"
    canonical.write_text(json.dumps({
        "schema_version": 1,
        "strategies": [{"key": "only_one", "display_name": "唯一",
                        "status": "candidate", "sharpe": 1.23,
                        "source_report": "reports/whatever.json",
                        "unexpected_field": "ignored"}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sc, "CANONICAL", canonical)

    rows, provisional = load_comparison()
    assert provisional is False
    assert [r.key for r in rows] == ["only_one"]
    assert rows[0].sharpe == pytest.approx(1.23)


def test_no_ranking_or_composite_score_is_exposed():
    """本模組刻意不提供排名/評分——期間與樣本單位不同，排名是假可比。"""
    for banned in ("rank", "score", "best", "winner"):
        assert not any(banned in name.lower()
                       for name in dir(sc) if not name.startswith("_"))
