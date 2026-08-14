"""Always-valid（sequential）推論，供 forward journal 每月監看用。

## 為什麼 forward 需要這個

固定樣本的 p 值只在**事先講好看幾次**時有效。forward journal 的本質是
「每個月看一次，看很多年」——若每次都用固定樣本 p<0.05 判斷，
光是反覆偷看就會讓型一錯誤率遠超過 5%。

Always-valid confidence sequence 的性質是：**整條路徑同時成立**。

    P( 存在某個 n 使得真值落在 CI(n) 之外 ) <= alpha

因此可以每個月看、可以在證據足夠時提早停止，而不破壞錯誤率保證。
代價是同一個 n 下區間比固定樣本寬——**它不會憑空製造 power**。

## 它解決與不解決的事

| | |
|---|---|
| 解決 | 「每月偷看」不再使結論失效；效果若很強，可以提早確認 |
| **不解決** | 效果若很弱，仍然需要很長時間。這是資訊量的物理限制 |

## 使用前必須事前固定的三件事

1. `sigma`：逐期噪音的規模。**用歷史資料估、事前寫死**，
   不得從正在檢定的 forward 資料估——那會讓 σ 隨結果漂移。
2. `alpha`：型一錯誤率。
3. `tightest_at`：希望區間在第幾期最緊（mixture 的調節參數 ρ = 1/tightest_at）。
   選它等於選「打算在什麼時間尺度上做決定」，**必須在看到任何 forward 資料前決定**。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# 常用分位數，避免為了三個數字引入 scipy
Z = {0.80: 0.8416212, 0.90: 1.2815516, 0.95: 1.6448536, 0.975: 1.9599640}


@dataclass(frozen=True)
class SequentialPlan:
    """事前登記的監看計畫。三個參數一旦開始累積 forward 就不得更動。"""

    sigma: float
    alpha: float = 0.05
    tightest_at: int = 60          # 期；預設 5 年，代表決策的時間尺度

    @property
    def rho(self) -> float:
        return 1.0 / float(self.tightest_at)


def confidence_sequence(values, plan: SequentialPlan) -> pd.DataFrame:
    """逐期回傳 always-valid 信賴序列（normal-mixture 邊界）。

    半徑

        r(n) = sigma * sqrt( 2(n*rho + 1) / (n^2 * rho) * log( sqrt(n*rho + 1) / alpha ) )

    大 n 時 ~ sigma*sqrt(log n / n)，比固定樣本的 sigma/sqrt(n) 慢一個 sqrt(log n)
    ——那就是「可以一直看」所付的價錢。
    """
    series = pd.Series(values, dtype=float).dropna()
    if series.empty:
        return pd.DataFrame(columns=["n", "mean", "radius", "lower", "upper",
                                     "excludes_zero"])
    n = np.arange(1, len(series) + 1, dtype=float)
    mean = series.to_numpy().cumsum() / n
    rho = plan.rho
    radius = plan.sigma * np.sqrt(
        (2.0 * (n * rho + 1.0) / (n ** 2 * rho))
        * np.log(np.sqrt(n * rho + 1.0) / plan.alpha))
    lower, upper = mean - radius, mean + radius
    return pd.DataFrame({
        "n": n.astype(int),
        "mean": mean,
        "radius": radius,
        "lower": lower,
        "upper": upper,
        # 整條路徑同時有效，所以「曾經排除 0」就是可以下結論的時刻
        "excludes_zero": (lower > 0) | (upper < 0),
    }, index=series.index)


def months_for_expected_t(effect: float, sigma: float, target_t: float = 1.96) -> int:
    """**規劃用**：若真實效果就是 ``effect``，期望 t 達到門檻需要幾期。

    這是「期望值剛好碰到門檻」，也就是大約**五成機率**會達標——
    不是 power。要 80% power 用 `months_for_power`。
    """
    if effect == 0:
        return -1
    return int(np.ceil((sigma * target_t / abs(effect)) ** 2))


def months_for_power(effect: float, sigma: float, *, alpha: float = 0.05,
                     power: float = 0.80, one_sided: bool = False) -> int:
    """**規劃用**：達到指定 power 需要幾期。"""
    if effect == 0:
        return -1
    z_alpha = Z[0.95] if one_sided else Z[0.975]
    return int(np.ceil(((z_alpha + Z[power]) * sigma / abs(effect)) ** 2))


def planning_horizon(effect: float, sigma: float) -> dict:
    """一次給出四個規劃數字。**這些是規劃，不是證據。**

    而且多半偏樂觀：``effect`` 來自已經被看過的歷史資料，存在 winner's curse；
    真實效果若較小，所需期數會急遽變長（與 effect 平方成反比）。
    """
    return {
        "expected_t_1.96_months": months_for_expected_t(effect, sigma, 1.96),
        "expected_t_1.645_months": months_for_expected_t(effect, sigma, 1.645),
        "power80_two_sided_months": months_for_power(effect, sigma),
        "power80_one_sided_months": months_for_power(effect, sigma, one_sided=True),
        "note": ("planning only, not evidence; effect comes from already-inspected "
                 "history (winner's curse) and required periods scale with 1/effect^2"),
    }
