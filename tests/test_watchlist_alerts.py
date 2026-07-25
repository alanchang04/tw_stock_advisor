"""聰明日級通知（方向 A）——diff / format 純函式測試，不碰 DB。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.watchlist_alerts import diff_rankings, format_ranking_alert


def _r(sid, rank, streak=0, rev=None, name="X"):
    return {"stock_id": sid, "stock_name": name, "rank": rank,
            "invest_streak": streak, "rev_yoy": rev}


# ── diff ─────────────────────────────────────────────────────────
def test_first_run_has_no_diff():
    d = diff_rankings([_r("2330", 1)], prev_ids=set())
    assert d["first_run"] and not d["entered"] and not d["dropped"]


def test_entered_and_dropped():
    today = [_r("2330", 1), _r("2454", 2)]
    d = diff_rankings(today, prev_ids={"2454", "6669"})
    assert [r["stock_id"] for r in d["entered"]] == ["2330"]   # 新進
    assert d["dropped"] == ["6669"]                            # 退榜
    assert d["first_run"] is False


def test_no_change_returns_empty():
    today = [_r("2330", 1), _r("2454", 2)]
    d = diff_rankings(today, prev_ids={"2330", "2454"})
    assert not d["entered"] and not d["dropped"]


# ── format ───────────────────────────────────────────────────────
def test_format_none_on_first_run():
    assert format_ranking_alert({"first_run": True}, "2026-07-25") is None


def test_format_none_when_no_change():
    assert format_ranking_alert(
        {"entered": [], "dropped": [], "first_run": False}, "2026-07-25") is None


def test_format_lists_entered_with_factors():
    d = {"first_run": False, "dropped": [],
         "entered": [_r("3706", 4, streak=6, rev=80.9, name="神達")]}
    out = format_ranking_alert(d, "2026-07-25")
    assert "新進榜" in out
    assert "3706 神達" in out
    assert "投信連買6日" in out and "營收+80.9%" in out


def test_star_marks_both_proven_factors():
    """投信連買≥3 且 營收>0 → 加⭐（兩個已驗證因子同時成立）。"""
    d = {"first_run": False, "dropped": [],
         "entered": [_r("3706", 4, streak=6, rev=80.9)]}
    assert "⭐" in format_ranking_alert(d, "2026-07-25")


def test_no_star_when_only_one_factor():
    d = {"first_run": False, "dropped": [],
         "entered": [_r("2330", 1, streak=0, rev=67.9)]}   # 營收好但投信沒連買
    out = format_ranking_alert(d, "2026-07-25")
    assert "2330" in out and "⭐" not in out


def test_format_lists_dropped_with_names():
    d = {"first_run": False, "entered": [], "dropped": ["6669"]}
    out = format_ranking_alert(d, "2026-07-25", dropped_names={"6669": "緯穎"})
    assert "退榜" in out and "6669 緯穎" in out


def test_format_always_includes_not_an_order_warning():
    d = {"first_run": False, "dropped": [], "entered": [_r("3706", 4, 6, 80.9)]}
    assert "不是買賣單" in format_ranking_alert(d, "2026-07-25")
