"""agent/research_status.py 的測試：只驗中繼資料解析與「不得洩漏績效」的性質。"""
from __future__ import annotations

import json

import pytest

from agent.research_status import (
    available_releases, component_table, describe_status, load_release,
    readiness_table, repeat_build_consistency,
)

PERF_TOKENS = ("return", "sharpe", "cagr", "drawdown", "nav", "pnl", "報酬", "回撤")


def test_loads_latest_release_and_required_fields():
    rel = load_release()
    assert rel.release_id.startswith("tw_stock_data_")
    assert rel.components, "釋出必須至少有一個元件"
    assert rel.file_count > 0
    assert len(rel.collection_sha256) == 64


def test_available_releases_sorted_newest_first():
    paths = available_releases()
    assert paths, "reports/data_releases/ 應有釋出定義"
    assert [p.name for p in paths] == sorted((p.name for p in paths), reverse=True)


def test_unknown_status_is_not_optimistically_ready():
    """未知的 component_status 必須視為未就緒——樂觀預設正是無聲污染的來源。"""
    label, ready = describe_status("some_future_status_we_have_not_seen")
    assert ready is False
    assert label == "some_future_status_we_have_not_seen"
    assert describe_status(None) == ("未標示", False)
    assert describe_status("")[1] is False


def test_component_table_exposes_no_performance_columns():
    rel = load_release()
    rows = component_table(rel)
    assert rows
    for row in rows:
        for key in row:
            assert not any(t in key.lower() for t in PERF_TOKENS), \
                f"研究進度頁不得出現績效欄位：{key}"


def test_readiness_table_covers_every_flag():
    rel = load_release()
    rows = readiness_table(rel)
    assert len(rows) == len(rel.readiness)
    for row in rows:
        assert row["狀態"].startswith(("✅", "🔒"))


def test_holdout_gate_is_closed_in_current_release():
    """holdout 尚未開封。這條測試若失敗，代表閘門狀態變了，必須由人確認而不是靜默通過。"""
    rel = load_release()
    assert rel.readiness.get("backward_holdout_performance_ready") is False


def test_repeat_build_consistency_matches():
    rel = load_release()
    rep = repeat_build_consistency(rel)
    if rep["available"]:
        assert rep["mismatched"] == [], "重複建構的 content SHA-256 必須完全一致"
        assert rep["matched"] == rep["checked"]


def test_missing_required_field_raises(tmp_path):
    bad = tmp_path / "broken_release.json"
    bad.write_text(json.dumps({"data_release_id": "x"}), encoding="utf-8")
    with pytest.raises(ValueError, match="缺少必要欄位"):
        load_release(bad)


def test_structural_failures_reported():
    rel = load_release()
    for cid in rel.structural_failures:
        assert any(c["component_id"] == cid for c in rel.components)
