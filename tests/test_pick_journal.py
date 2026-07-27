"""前向計分板——summarize_scoreboard 純函式測試（不碰 DB）。

核心指標 edge_vs_pool（你的挑選 − 前20整體）決定「你的判斷有沒有加分」，
配對/缺值處理若錯，整個結論會歪，所以重點測這裡。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.pick_journal import summarize_scoreboard


def _r(pick, pool, mkt):
    return {"pick_ret": pick, "pool_ret": pool, "mkt_ret": mkt}


def test_empty():
    s = summarize_scoreboard([])
    assert s["n"] == 0 and s["avg_pick"] is None and s["edge_vs_pool"] is None


def test_judgment_adds_value_over_pool():
    # 你的挑選平均 +10%，前20整體 +4% → 加分 +6pp，且兩筆都贏
    s = summarize_scoreboard([_r(0.12, 0.05, 0.03), _r(0.08, 0.03, 0.03)])
    assert abs(s["avg_pick"] - 0.10) < 1e-9
    assert abs(s["edge_vs_pool"] - 0.06) < 1e-9
    assert s["win_vs_pool"] == 2 and s["n_vs_pool"] == 2


def test_judgment_subtracts_value():
    # 你挑的比整份清單差 → edge 為負（你的過濾幫倒忙）
    s = summarize_scoreboard([_r(0.01, 0.05, 0.02), _r(-0.02, 0.04, 0.02)])
    assert s["edge_vs_pool"] < 0
    assert s["win_vs_pool"] == 0


def test_edge_vs_market():
    s = summarize_scoreboard([_r(0.10, 0.05, 0.03), _r(0.06, 0.05, 0.04)])
    # (0.10-0.03)+(0.06-0.04) = 0.07+0.02 = 0.09；平均 0.045
    assert abs(s["edge_vs_mkt"] - 0.045) < 1e-9


def test_none_values_are_skipped_pairwise():
    """pool 缺值的那筆，不該污染 edge_vs_pool（配對計算），但仍計入 avg_mkt。"""
    rows = [_r(0.10, None, 0.02), _r(0.08, 0.03, 0.02)]
    s = summarize_scoreboard(rows)
    assert s["n"] == 2
    assert s["n_vs_pool"] == 1                         # 只有一筆 pool 有值
    assert abs(s["edge_vs_pool"] - 0.05) < 1e-9        # 0.08-0.03
    assert abs(s["avg_pick"] - 0.09) < 1e-9            # 兩筆 pick 都算


def test_avg_pool_only_over_available():
    s = summarize_scoreboard([_r(0.1, None, 0.0), _r(0.2, 0.04, 0.0)])
    assert abs(s["avg_pool"] - 0.04) < 1e-9            # 只有一筆 pool 有值


def test_win_count_strict_greater():
    """平手（pick == pool）不算贏。"""
    s = summarize_scoreboard([_r(0.05, 0.05, 0.0)])
    assert s["win_vs_pool"] == 0 and s["n_vs_pool"] == 1
