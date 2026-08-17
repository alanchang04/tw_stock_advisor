"""agent/evidence.py 的測試：未標記的數字不得被當成任何等級的證據。"""
from __future__ import annotations

from agent.evidence import (TIERS, UNKNOWN, annotate, badge, caption,
                            is_confirmation, tier)


def test_known_tiers_cover_the_documented_vocabulary():
    for key in ("development", "backward", "sealed", "forward", "inconclusive"):
        assert key in TIERS
        assert TIERS[key].label
        assert TIERS[key].meaning


def test_unknown_tier_is_never_treated_as_confirmation():
    """樂觀預設是無聲污染的來源——沒標記就不能算證據。"""
    for bad in (None, "", "   ", "made_up_tier", "DEVELOPMENTAL"):
        assert tier(bad) is UNKNOWN or tier(bad).key == "unknown", bad
        assert is_confirmation(bad) is False, bad


def test_only_backward_and_forward_count_as_confirmation():
    assert is_confirmation("backward") is True
    assert is_confirmation("forward") is True
    # development 是最容易被誤當確認的那一個
    assert is_confirmation("development") is False
    assert is_confirmation("sealed") is False
    assert is_confirmation("inconclusive") is False


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
