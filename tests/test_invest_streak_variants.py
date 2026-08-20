"""P3-11 替代的投信連買定義。

現行定義的兩個病態（實測皆會發生）：
  ①單日淨賣 20 張就把「連買 1000 張 × 10 天」歸零——61.9% 的中斷屬此類
  ②每天只買 5 張也算連買，只要累計過 100 張

這組測試用手工序列把兩種定義的行為釘死，重點在**邊界**：容忍到什麼程度停止容忍、
以及量體門檻擋不擋得住「靠小額堆出來」的假 streak。
"""
import numpy as np
import pandas as pd

from research.invest_streak_variants import (
    streak_with_size_floor,
    streak_with_tolerance,
)


def _frame(values, name="A"):
    idx = pd.date_range("2020-01-01", periods=len(values), freq="D").date
    return pd.DataFrame({name: values}, index=idx)


def test_tolerance_survives_a_trivial_sell():
    """連買 1000 張 × 5 天後賣 20 張——整體立場沒變，streak 不該歸零。"""
    inv = _frame([1000, 1000, 1000, 1000, 1000, -20, 1000])
    out = streak_with_tolerance(inv)["A"].tolist()

    assert out[4] == 5
    assert out[5] == 5, "被容忍的賣出日不重置，也不增加長度"
    assert out[6] == 6, "容忍後下一個買超日繼續累加"


def test_tolerance_stops_at_a_meaningful_sell():
    """賣超超過該段平均買超的 10% 就是真的轉向，必須歸零。"""
    inv = _frame([1000, 1000, 1000, 1000, 1000, -300, 1000])
    out = streak_with_tolerance(inv)["A"].tolist()

    assert out[4] == 5
    assert out[5] == 0, "賣 300 張 > 平均買超 1000 張的 10%，屬真實轉向"
    assert out[6] == 1, "重置後重新起算；單日 1000 張已過 100 張量體門檻，故長度為 1"


def test_tolerance_is_relative_not_absolute():
    """同樣賣 20 張，對小額連買而言是重大轉向，不該被容忍。"""
    small = _frame([50, 50, 50, -20])
    out = streak_with_tolerance(small)["A"].tolist()

    assert out[3] == 0, "20 張 > 50 張的 10%，屬真實轉向"


def test_tolerance_blocks_the_degenerate_buy100_sell10_loop():
    """只加容忍而仍只累計買超，會讓『買100賣10』無限延續；改用淨額才擋得住。"""
    inv = _frame([100, -10] * 30)
    out = streak_with_tolerance(inv)["A"]

    net = sum([100, -10] * 30)
    assert net > 0
    # 淨額確實在成長，所以 streak 可以成立——但長度只計買超日，不會虛胖成 60
    assert out.max() <= 30


def test_size_floor_rejects_token_buying():
    """每天只買佔成交量 0.1% 的量，是試水溫不是進場。"""
    inv = _frame([1000] * 10)
    vol = _frame([1_000_000] * 10)          # 買超僅佔 0.1%
    out = streak_with_size_floor(inv, vol)["A"]

    assert (out == 0).all()


def test_size_floor_accepts_meaningful_buying():
    inv = _frame([1000] * 10)
    vol = _frame([10_000] * 10)             # 買超佔 10%，高於 5% 門檻
    out = streak_with_size_floor(inv, vol)["A"].tolist()

    assert out[-1] == 10


def test_size_floor_breaks_on_a_below_threshold_day():
    inv = _frame([1000, 1000, 10, 1000])
    vol = _frame([10_000] * 4)
    out = streak_with_size_floor(inv, vol)["A"].tolist()

    assert out[1] == 2
    assert out[2] == 0, "未達量體門檻的日子視同中斷"


def test_nan_inputs_do_not_crash_or_extend():
    inv = _frame([1000, np.nan, 1000])
    vol = _frame([10_000, 10_000, 10_000])

    assert not streak_with_tolerance(inv)["A"].isna().any()
    assert not streak_with_size_floor(inv, vol)["A"].isna().any()


def test_output_shape_matches_input():
    """要能直接塞回 data['_inv_streak']，形狀必須一致。"""
    inv = pd.DataFrame({"A": [100, 200], "B": [300, 400]},
                       index=pd.date_range("2020-01-01", periods=2).date)
    vol = pd.DataFrame({"A": [1000, 1000], "B": [1000, 1000]}, index=inv.index)

    for out in (streak_with_tolerance(inv), streak_with_size_floor(inv, vol)):
        assert list(out.columns) == ["A", "B"]
        assert list(out.index) == list(inv.index)
