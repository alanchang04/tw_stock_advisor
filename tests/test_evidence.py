"""agent/evidence.py 的測試：未標記的數字不得被當成任何等級的證據。"""
from __future__ import annotations

from agent.evidence import (TIERS, UNKNOWN, annotate, badge, caption,
                            invalidated_artifacts, is_confirmation, load_registry,
                            tier)


def test_vocabulary_comes_from_the_registry_not_a_local_copy():
    """權威來源是研究機的 evidence_registry.json，不是前端自己的表。"""
    from agent.evidence import REGISTRY, TIER_SOURCE
    assert REGISTRY.is_file(), "註冊表應已由研究機交付"
    assert TIER_SOURCE == "evidence_registry"


def test_registry_exposes_the_research_owned_invalidated_artifact_list():
    payload = load_registry()
    rows = invalidated_artifacts()
    assert rows == payload["invalidated_artifacts"]
    assert len(rows) == 5
    assert all({"artifact", "invalidated_at", "reason", "superseded_by"} <= row.keys()
               for row in rows)


def test_known_tiers_cover_the_registry_vocabulary():
    for key in ("development", "backward", "holdout_spent", "sealed", "forward"):
        assert key in TIERS, f"註冊表詞彙 {key} 沒被載入"
        assert TIERS[key].label
        assert TIERS[key].meaning


def test_new_tier_defaults_to_not_confirmation():
    """研究機日後新增等級時，前端沒更新樣式也不得把它當成確認證據。"""
    from agent.evidence import _STYLE
    unknown_to_style = set(TIERS) - set(_STYLE)
    for key in unknown_to_style:
        assert TIERS[key].is_confirmation is False


def test_unknown_tier_is_never_treated_as_confirmation():
    """樂觀預設是無聲污染的來源——沒標記就不能算證據。"""
    for bad in (None, "", "   ", "made_up_tier", "DEVELOPMENTAL"):
        assert tier(bad) is UNKNOWN or tier(bad).key == "unknown", bad
        assert is_confirmation(bad) is False, bad


def test_only_backward_forward_and_spent_holdout_count_as_confirmation():
    assert is_confirmation("backward") is True
    assert is_confirmation("forward") is True
    assert is_confirmation("holdout_spent") is True
    # development 是最容易被誤當確認的那一個
    assert is_confirmation("development") is False
    assert is_confirmation("sealed") is False


def test_tier_lookup_is_case_and_space_tolerant():
    assert tier("Development").key == "development"
    assert tier("CLEAN BACKWARD").key == "unknown", "label 不是 key，不該誤命中"
    assert tier(" backward ").key == "backward"


def test_annotate_keeps_the_number_and_adds_the_badge():
    out = annotate("0.92", "development")
    assert "0.92" in out
    assert "DEVELOPMENT" in out


def test_annotate_marks_unlabelled_numbers_rather_than_staying_silent():
    """沒給等級時必須顯示 UNVERIFIED，不能安靜地只印數字。"""
    out = annotate("0.92", None)
    assert "0.92" in out
    assert "UNVERIFIED" in out


def test_badge_is_markdown_safe():
    for key in list(TIERS) + [None]:
        b = badge(key)
        assert "`" in b
        assert "\n" not in b


def test_caption_explains_why_the_tier_matters():
    assert "不是" in caption("development")
    assert "一次" in caption("backward")
