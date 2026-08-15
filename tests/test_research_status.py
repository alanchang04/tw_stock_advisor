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


def test_holdout_was_opened_exactly_once_and_must_not_reopen():
    """holdout 已於 r3 開封（2026-08-14，使用者明示同意），**只此一次**。

    這條測試原本斷言閘門仍關閉。開封之後它就一直是紅的——那是設計如此：
    閘門狀態改變必須由人確認，不得靜默通過。使用者已確認，因此不變量
    改成新的那一個：**開封過了，而且不准再開第二次。**

    「不准再開第二次」的意思是：`tw_stock_data_2005_2014_r3` 是最後一個
    改動這個旗標的釋出。任何**新的**釋出若再次把它從 False 翻成 True，
    代表有人重跑了 backward holdout——那會摧毀它作為一次性驗收的價值，
    這條測試必須先變紅。
    """
    rel = load_release()
    assert rel.readiness.get("backward_holdout_performance_ready") is True, (
        "閘門狀態又變了。若這是**新的**一次開封，等於重跑 holdout——"
        "那是不允許的；若是回退到未開封，請說明原因。")
    assert rel.release_id == "tw_stock_data_2005_2014_r3", (
        f"開封發生在 r3，但目前釋出是 {rel.release_id}。"
        "換釋出時必須由人確認 holdout 沒有被重跑。")


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
