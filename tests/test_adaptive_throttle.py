"""
訊號品質偵測+動態縮手測試（agent/backtest.py _adaptive_throttle_blocked，
SPEC_QUANT_UPGRADE.md：診斷2021/2024異常虧損年份後新增）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.backtest import _adaptive_throttle_blocked
from agent.strategy import STRATEGY


def cfg(**overrides):
    return {**STRATEGY, "adaptive_throttle_enabled": True,
           "adaptive_throttle_lookback": 10, "adaptive_throttle_min_trades": 10,
           "adaptive_throttle_win_rate": 0.20, **overrides}


def test_disabled_by_default_never_blocks():
    # STRATEGY 預設 adaptive_throttle_enabled=False（未經10年A/B驗證前不影響現行行為）
    assert STRATEGY.get("adaptive_throttle_enabled") is False
    bad_streak = [False] * 20
    assert _adaptive_throttle_blocked(bad_streak, STRATEGY) is False


def test_not_enough_trades_yet_does_not_block():
    # 累積不到 min_trades 筆平倉紀錄前，即使全輸也不擋（避免暖身期誤觸發）
    recent_wins = [False] * 9
    assert _adaptive_throttle_blocked(recent_wins, cfg()) is False


def test_low_win_rate_blocks_after_min_trades():
    # 10筆全輸，勝率0% < 20%門檻 → 擋
    recent_wins = [False] * 10
    assert _adaptive_throttle_blocked(recent_wins, cfg()) is True


def test_healthy_win_rate_does_not_block():
    # 10筆裡3筆贏，勝率30% >= 20%門檻 → 不擋
    recent_wins = [True, False, True, False, False, True, False, False, False, False]
    assert _adaptive_throttle_blocked(recent_wins, cfg()) is False


def test_only_looks_at_lookback_window_not_full_history():
    # 前面輸很多筆，但最近 lookback(10) 筆勝率健康 → 不擋（只看最近，不是全歷史）
    recent_wins = [False] * 30 + [True] * 5 + [False] * 3 + [True] * 2   # 最近10筆: 3輸+5贏+... 算勝率
    last10 = recent_wins[-10:]
    expected_blocked = (sum(last10) / len(last10)) < 0.20
    assert _adaptive_throttle_blocked(recent_wins, cfg()) == expected_blocked
    assert expected_blocked is False   # 最近10筆勝率明顯高於20%


def test_exactly_at_threshold_boundary_not_blocked():
    # 10筆裡2筆贏，勝率剛好20% —— 用 < 不是 <=，等於門檻不觸發
    recent_wins = [True, True] + [False] * 8
    assert _adaptive_throttle_blocked(recent_wins, cfg()) is False


def test_just_below_threshold_blocks():
    # 10筆裡1筆贏，勝率10% < 20% → 擋
    recent_wins = [True] + [False] * 9
    assert _adaptive_throttle_blocked(recent_wins, cfg()) is True
