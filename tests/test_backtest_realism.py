"""
回測寫實度測試（agent/backtest.py，2026-07-15 人類交易員練習軌規格附帶的回測優化）：
跌停鎖死偵測 is_limit_locked() 純函式。不需 DB。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.backtest import is_limit_locked
from agent.strategy import STRATEGY


def cfg(**overrides):
    return {**STRATEGY, **overrides}


def test_limit_locked_when_down_and_thin_volume():
    # 跌9.6%（≤-9.5%門檻）且量僅均量的5%（<10%門檻）→ 鎖死
    assert is_limit_locked(-9.6, 500, 10_000, cfg()) is True


def test_not_locked_when_down_but_normal_volume():
    # 跌停但量正常（跌停打開，真的有成交）→ 不算鎖死
    assert is_limit_locked(-9.6, 5_000, 10_000, cfg()) is False


def test_not_locked_when_thin_volume_but_not_down_much():
    # 量很小但跌幅不到門檻 → 不算鎖死（只是清淡交易，不是跌停）
    assert is_limit_locked(-3.0, 100, 10_000, cfg()) is False


def test_locked_at_exact_threshold_boundary():
    # 門檻定義是「≤-9.5%」，剛好等於門檻也算數（含邊界）
    assert is_limit_locked(-9.5, 100, 10_000, cfg()) is True

def test_not_locked_just_above_threshold():
    assert is_limit_locked(-9.4, 100, 10_000, cfg()) is False


def test_not_locked_when_missing_data():
    assert is_limit_locked(None, 500, 10_000, cfg()) is False
    assert is_limit_locked(-9.6, None, 10_000, cfg()) is False
    assert is_limit_locked(-9.6, 500, None, cfg()) is False
    assert is_limit_locked(-9.6, 500, 0, cfg()) is False


def test_limit_locked_threshold_configurable():
    loose = cfg(limit_down_pct=-0.05, limit_lock_vol_ratio=0.5)
    assert is_limit_locked(-6.0, 4_000, 10_000, loose) is True   # 5%門檻+50%量門檻都觸發
