"""回測結果的 df.attrs 不可以放 pandas 物件（2026-07-24 實際踩過的 bug）。

pandas 的 `NDFrame.__finalize__` 在 concat 時會執行 `obj.attrs == attrs` 比對，
若 attrs 的值是 Series/DataFrame/ndarray，`==` 回傳的是逐元素結果而不是 bool，
於是 `all(...)` 拋 "The truth value of a Series is ambiguous"。

後果是任何觸發 __finalize__ 的常規操作都會炸——nlargest、concat、部分 groupby——
而且錯誤訊息完全指不到真正的原因（堆疊全在 pandas 內部）。這種地雷值得一條測試。
"""
import numpy as np
import pandas as pd
import pytest


def _attrs_like_backtest():
    """複製 run_backtest 會塞進 attrs 的形狀（值型別才是重點，數值無所謂）。"""
    idx = pd.date_range("2020-01-01", periods=5)
    return {
        "nav_total_ret": 3.28, "nav_mdd": -0.274, "sharpe": 0.97,
        "start": idx[0].date(), "end": idx[-1].date(),
        "nav": {d.date(): float(v) for d, v in zip(idx, range(5))},
        "nav_0050": None,
    }


def test_backtest_attrs_contain_no_pandas_objects():
    for k, v in _attrs_like_backtest().items():
        assert not isinstance(v, (pd.Series, pd.DataFrame, pd.Index, np.ndarray)), \
            f"attrs['{k}'] 是 {type(v).__name__}——會讓 concat/nlargest 炸掉，請改存 dict"


def test_dataframe_with_these_attrs_survives_nlargest():
    df = pd.DataFrame({"stock_id": list("abcde"), "net_ret": [0.1, -0.2, 0.3, np.nan, 0.05]})
    df.attrs.update(_attrs_like_backtest())
    assert list(df.nlargest(2, "net_ret")["stock_id"]) == ["c", "a"]


def test_dataframe_with_these_attrs_survives_concat():
    df = pd.DataFrame({"a": [1, 2]})
    df.attrs.update(_attrs_like_backtest())
    assert len(pd.concat([df, df])) == 4


def test_series_in_attrs_really_does_break_concat():
    """反向證明：這不是想像出來的風險，放 Series 真的會炸。"""
    df = pd.DataFrame({"a": [1, 2]})
    df.attrs["nav"] = pd.Series([1.0, 2.0])
    with pytest.raises(ValueError, match="truth value"):
        pd.concat([df, df.copy()])


def test_nav_dict_round_trips_to_series():
    """存 dict 不影響用途：拿回來一行就變回 Series。"""
    s = pd.Series(_attrs_like_backtest()["nav"])
    assert len(s) == 5 and s.iloc[-1] == 4.0
