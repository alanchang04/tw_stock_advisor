"""
族群曝險上限接進live每日推薦流程的測試（agent/stock_selector.py _apply_sector_cap，
SPEC_QUANT_UPGRADE.md P3決策3：10年回測驗證過的邏輯，2026-07-20補接到即時推薦，
之前只有回測有這段，造成線上/回測不一致）。純函式，不需要DB。
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from agent.stock_selector import _apply_sector_cap


def _df(rows):
    return pd.DataFrame(rows)


def test_no_cap_hit_returns_top_n_unchanged():
    df = _df([{"stock_id": f"S{i}", "score": 10 - i, "industry": "A"} for i in range(3)])
    out = _apply_sector_cap(df, Counter(), top_n=3, max_per_sector=5)
    assert list(out["stock_id"]) == ["S0", "S1", "S2"]


def test_skips_candidate_that_would_exceed_cap():
    # 3檔都是族群A，上限只給2檔 → 第3名(分數最低)被跳過，沒有下一名可以遞補
    df = _df([{"stock_id": f"S{i}", "score": 10 - i, "industry": "A"} for i in range(3)])
    out = _apply_sector_cap(df, Counter(), top_n=3, max_per_sector=2)
    assert list(out["stock_id"]) == ["S0", "S1"]


def test_replacement_from_lower_ranked_different_sector():
    # 前2名都是A族群(上限1)，第3名擋掉，第4名是B族群才遞補進來
    df = _df([
        {"stock_id": "A1", "score": 10, "industry": "A"},
        {"stock_id": "A2", "score": 9,  "industry": "A"},
        {"stock_id": "B1", "score": 8,  "industry": "B"},
    ])
    out = _apply_sector_cap(df, Counter(), top_n=2, max_per_sector=1)
    assert list(out["stock_id"]) == ["A1", "B1"]


def test_already_held_positions_count_toward_cap():
    # 族群A已經持有2檔(sector_counts起始值)，上限2 → 候選池裡的A族群股票直接被擋
    df = _df([
        {"stock_id": "A1", "score": 10, "industry": "A"},
        {"stock_id": "B1", "score": 9,  "industry": "B"},
    ])
    out = _apply_sector_cap(df, Counter({"A": 2}), top_n=5, max_per_sector=2)
    assert list(out["stock_id"]) == ["B1"]


def test_missing_industry_never_capped():
    df = _df([{"stock_id": "S1", "score": 10, "industry": None}] * 5)
    out = _apply_sector_cap(df, Counter(), top_n=5, max_per_sector=1)
    assert len(out) == 5   # 沒有族群資料的一律放行，不誤擋


def test_stops_at_top_n_even_with_room_left_in_sectors():
    df = _df([{"stock_id": f"S{i}", "score": 10 - i, "industry": f"SEC{i}"} for i in range(10)])
    out = _apply_sector_cap(df, Counter(), top_n=3, max_per_sector=5)
    assert len(out) == 3
