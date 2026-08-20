"""TWSE 漲跌停價與「鎖死」狀態的判定（2005~2014）。

為什麼需要這個模組
------------------
`SPEC_DATA_FOUNDATION_AND_MOMENTUM.md` §7.5 規定「漲跌停鎖死或停止交易時不得
假成交」，但沒有定義什麼叫鎖死。`research/momentum_execution.py` 因此要求呼叫端
傳入明確的 `locked_limit`（`None`／`"up"`／`"down"`），而 F0 診斷先前只能記錄
`official_locked_limit_state_available: False`。

官方沒有「是否鎖死」這個欄位，所以這件事**不是下載問題**，是判定規則問題。

為什麼不用 change_pct 門檻
--------------------------
最直覺的做法是「`change_pct >= 6.9` 即漲停」。2026-08-12 實測否決了它：
2005~2014 共 1,917,768 筆價格中，`change_pct` 在 6.5~7.0 之間是**平滑分布、
沒有斷點**（[6.85,6.90) 有 10,398 筆、[6.90,6.95) 有 12,572 筆）。
原因是漲停價要取到合法檔位，跨級距時會明顯低於 7%——例如參考價 9.99 元，
漲停價落在 0.05 檔位區間而成為 10.65 元，僅 +6.61%。
以 6.9% 為門檻會漏掉 **54%** 的真實漲停日。任何門檻都是啟發式，
不符 AGENTS.md「不得發明代理指標」。

改用的做法
----------
由官方 `change_pct` 反推當日參考價，再套官方升降單位精確計算漲跌停價。
`change_pct` 是交易所相對**當日參考價**計算的，因此這個反推同時避開了 D3
記錄的 182 筆「參考價重設」問題——不需要自己去找前一個收盤價。

實測驗證：限制在 MOM-1 的 `close >= 10` 範圍內，被判為漲停的 39,760 筆
`change_pct` 落在 6.496~7.000，下界與跨級距進位的理論值一致。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# TWSE 股票升降單位（2005~2014），(價格上界, 檔位)
TICK_BANDS: tuple[tuple[float, float], ...] = (
    (10.0, 0.01),
    (50.0, 0.05),
    (100.0, 0.1),
    (500.0, 0.5),
    (1000.0, 1.0),
)
TICK_ABOVE_TOP_BAND = 5.0
DAILY_LIMIT_FRACTION = 0.07      # 2005~2014 為 ±7%；2015-06-01 起改為 ±10%
_EPSILON = 1e-9
_PRICE_TOLERANCE = 1e-6


def tick_size(price: np.ndarray | pd.Series | float) -> np.ndarray:
    """回傳各價格所屬級距的升降單位。檔位由**該價格本身**決定，不是參考價。"""
    values = np.asarray(price, dtype=float)
    conditions = [values < upper for upper, _ in TICK_BANDS]
    choices = [tick for _, tick in TICK_BANDS]
    return np.select(conditions, choices, default=TICK_ABOVE_TOP_BAND)


def reference_price(close: pd.DataFrame | pd.Series,
                    change_pct: pd.DataFrame | pd.Series) -> np.ndarray:
    """由收盤價與官方漲跌幅反推當日參考價。

    交易所的 `change_pct` 是相對當日參考價計算的，因此這裡得到的就是交易所
    當天實際採用的分母；不必自行推測前一個收盤價，也就不會踩到 D3 記錄的
    參考價重設問題。
    """
    return np.asarray(close, dtype=float) / (1.0 + np.asarray(change_pct, dtype=float) / 100.0)


def limit_prices(reference: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """回傳 (漲停價, 跌停價)。漲停取不超過的最接近檔位，跌停取不低於的最接近檔位。"""
    reference = np.asarray(reference, dtype=float)
    raw_up = reference * (1.0 + DAILY_LIMIT_FRACTION)
    raw_down = reference * (1.0 - DAILY_LIMIT_FRACTION)
    tick_up = tick_size(raw_up)
    tick_down = tick_size(raw_down)
    upper = np.floor(raw_up / tick_up + _EPSILON) * tick_up
    lower = np.ceil(raw_down / tick_down - _EPSILON) * tick_down
    return upper, lower


def locked_limit_frame(
    *,
    open_: pd.DataFrame,
    high: pd.DataFrame,
    low: pd.DataFrame,
    close: pd.DataFrame,
    change_pct: pd.DataFrame,
) -> pd.DataFrame:
    """回傳與價格同形狀的鎖死狀態表，值為 ``"up"``／``"down"``／``None``。

    判定為鎖死需同時成立：

    1. 收盤價**精確等於**由官方檔位規則算出的漲停價（或跌停價）；且
    2. 當日 ``open == high == low == close``，亦即整個交易日只有一個成交價。

    第 2 條是關鍵。若當日曾在漲停價**以外**成交，代表有對手盤，開盤市價單
    本來就會成交，沒有理由阻擋。只有整日單一價才代表排隊未消化——此時假設
    自己的委託成交，是往樂觀方向犯錯。

    **退化情況**：若漲停價與參考價相差不足一個檔位（極低價股，7% 小於一檔），
    漲跌停的概念本身退化，一律回傳 ``None`` 而不硬套。MOM-1 的 §7.1.4
    要求收盤價 10 元以上，實務上不會遇到，但規則必須明確而不是碰運氣。
    """
    frames = {"open": open_, "high": high, "low": low, "close": close, "change_pct": change_pct}
    reference_shape = close.shape
    for name, frame in frames.items():
        if frame.shape != reference_shape:
            raise ValueError(f"{name} 形狀與 close 不一致，無法逐格判定漲跌停")
        if not frame.index.equals(close.index) or not frame.columns.equals(close.columns):
            raise ValueError(f"{name} 的 index/columns 與 close 不一致")

    close_values = close.to_numpy(dtype=float)
    reference = reference_price(close_values, change_pct.to_numpy(dtype=float))
    upper, lower = limit_prices(reference)
    reference_tick = tick_size(reference)

    single_price = (
        (open_.to_numpy(dtype=float) == high.to_numpy(dtype=float))
        & (high.to_numpy(dtype=float) == low.to_numpy(dtype=float))
        & (low.to_numpy(dtype=float) == close_values)
    )
    # 退化保護：漲跌停必須離參考價至少半個檔位才有意義。
    meaningful_up = (upper - reference) >= reference_tick * 0.5
    meaningful_down = (reference - lower) >= reference_tick * 0.5

    at_upper = (np.abs(close_values - upper) < _PRICE_TOLERANCE) & meaningful_up
    at_lower = (np.abs(close_values - lower) < _PRICE_TOLERANCE) & meaningful_down
    observed = np.isfinite(close_values) & np.isfinite(reference)

    state = np.full(close_values.shape, None, dtype=object)
    state[observed & single_price & at_upper] = "up"
    state[observed & single_price & at_lower] = "down"
    return pd.DataFrame(state, index=close.index, columns=close.columns)
