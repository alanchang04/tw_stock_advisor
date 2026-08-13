"""統計推論的修正層（`docs/RESEARCH_METHOD_AMENDMENTS.md` M1／M2／M5）。

這個模組存在的理由是三個**已確認的方法缺陷**，不是為了多一套工具：

M1 — block 長度與觀察窗不匹配
------------------------------
`research/event_study.py` 的 block 固定 20 個交易日，卻用來檢定 40／60／120 日
的累積報酬。120 日的結果本身就跨越 6 個 block，block 內獨立的假設不成立。
block 長度不是可以隨手填一個數字的常數，它是**需要做敏感度分析的估計量**。
因此任何以 block bootstrap 得到的 p 值，都必須附上 `block_length_sensitivity`
的掃描結果；若 p 隨 block 長度大幅漂移，該 p 值不得單獨引用。

M2 — 多重觀察窗本身就是多重檢定
--------------------------------
6 個因子 × 6 個窗＝36 格。在 α=0.05 下，即使全部因子皆無效，期望也會有
1.8 格「顯著」。因此「36 格裡有 1 格顯著」**不是證據，是預期值**。
`multiple_testing_budget` 把這件事算出來，不讓它停留在直覺。

M5 — 檢定的虛無假設應該是經濟門檻，不是零
------------------------------------------
台股一次完整換手成本 1.185%。「α 顯著大於 0」對決策毫無意義——
α=+0.4% 即使 p<0.001 也是賠錢的。正確的虛無假設是 `H0: α <= θ_min`，
其中 θ_min = 成本 + 安全邊際。`economic_significance` 檢定這個。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.event_study import block_bootstrap_ci

# 台股一次完整換手（證交稅 0.300% + 雙邊手續費 0.285% + 30bp 雙邊滑價 0.600%）
ROUND_TRIP_COST = 0.01185
# 安全邊際：成本模型本身有誤差（滑價與市況相關，見 M8），要求 edge 至少多出 1%
SAFETY_MARGIN = 0.01
MIN_TRADEABLE_EDGE = ROUND_TRIP_COST + SAFETY_MARGIN

# 掃描用的 block 長度。涵蓋最短窗（5 日）到最長窗（120 日）的量級。
SENSITIVITY_BLOCKS = (5, 20, 40, 60, 120)


@dataclass(frozen=True)
class SensitivityResult:
    """同一組資料在不同 block 長度下的推論。"""

    window: int
    by_block: dict[int, dict[str, float]]

    @property
    def p_values(self) -> list[float]:
        return [v["p_value"] for v in self.by_block.values()]

    @property
    def p_range(self) -> float:
        finite = [p for p in self.p_values if np.isfinite(p)]
        return float(max(finite) - min(finite)) if finite else float("nan")

    def verdict_is_stable(self, alpha: float = 0.05) -> bool:
        """所有 block 長度是否給出同一個顯著／不顯著判決。

        判決不穩定時，該窗的 p 值**不得**單獨引用——它主要反映的是
        block 長度這個任意選擇，而不是資料。
        """
        finite = [p for p in self.p_values if np.isfinite(p)]
        if not finite:
            return False
        return all(p < alpha for p in finite) or all(p >= alpha for p in finite)


def block_length_sensitivity(
    per_event: pd.DataFrame,
    window: int,
    *,
    sessions: pd.DatetimeIndex,
    blocks=SENSITIVITY_BLOCKS,
    draws: int = 1000,
    seed: int = 20260813,
) -> SensitivityResult:
    """同一組事件、同一個窗，掃描 block 長度後回報 p 值如何變動。

    直接沿用 `event_study.block_bootstrap_ci`，只改 block 長度，
    因此差異可完全歸因於 block 這一個選擇，不混入其他實作差異。
    """
    by_block: dict[int, dict[str, float]] = {}
    for block in blocks:
        low, high, p = block_bootstrap_ci(
            per_event, window, sessions=sessions,
            block_sessions=block, draws=draws, seed=seed,
        )
        by_block[int(block)] = {"ci_low": low, "ci_high": high, "p_value": p}
    return SensitivityResult(window=int(window), by_block=by_block)


def stationary_bootstrap_ci(
    per_event: pd.DataFrame,
    window: int,
    *,
    sessions: pd.DatetimeIndex,
    mean_block_sessions: int = 60,
    draws: int = 1000,
    seed: int = 20260813,
) -> tuple[float, float, float]:
    """Politis-Romano stationary bootstrap，回傳 (ci_low, ci_high, p_value)。

    與固定長度 block 的兩個差別，都是刻意的：

    1. **block 長度服從幾何分布**（平均 ``mean_block_sessions``），
       因此結果不繫於某一個任意的長度選擇——這正是 M1 的痛點。
    2. **在完整交易日曆上重抽**，包含沒有事件的交易日。固定 block 版本只重抽
       「有事件的 block」，等於假設事件密度隨時間固定；實際上事件密度本身
       就隨市況起伏（多頭時訊號多），那是真實的相關性來源之一。

    因為只需要重抽後的平均，這裡不物化個別報酬，改以每個交易日的
    (總和, 筆數) 聚合後向量化計算，`draws` 開到 1000 仍是秒級。
    """
    column = f"car_{window}"
    frame = per_event.dropna(subset=[column])
    if frame.empty:
        return (np.nan, np.nan, np.nan)

    calendar = pd.Series(range(len(sessions)), index=pd.DatetimeIndex(sessions))
    position = calendar.reindex(pd.DatetimeIndex(frame["event_date"]))
    if position.isna().any():
        raise ValueError("事件日不在交易日曆內，無法分塊")

    n_sessions = len(sessions)
    pos = position.to_numpy().astype(int)
    values = frame[column].to_numpy(dtype=float)
    sums = np.bincount(pos, weights=values, minlength=n_sessions)
    counts = np.bincount(pos, minlength=n_sessions).astype(float)

    rng = np.random.default_rng(seed)
    p_continue = 1.0 - 1.0 / float(mean_block_sessions)
    means = np.empty(draws, dtype=float)
    for i in range(draws):
        # 幾何長度的 block 串起來覆蓋整條日曆：先抽起點，再抽長度，
        # 以 mod 環繞（stationary bootstrap 的定義）保持定態性。
        starts = rng.integers(0, n_sessions, size=n_sessions)
        lengths = rng.geometric(1.0 - p_continue, size=n_sessions)
        keep = np.searchsorted(np.cumsum(lengths), n_sessions) + 1
        starts, lengths = starts[:keep], lengths[:keep]
        # 每個 block 內的 0..len-1 偏移，向量化展開（避免逐 block 迴圈）
        ends = np.cumsum(lengths)
        offsets = np.arange(ends[-1]) - np.repeat(ends - lengths, lengths)
        base = np.repeat(starts, lengths)
        sequence = (base + offsets)[:n_sessions] % n_sessions
        total = counts[sequence].sum()
        means[i] = sums[sequence].sum() / total if total else np.nan

    means = means[np.isfinite(means)]
    if means.size == 0:
        return (np.nan, np.nan, np.nan)
    low, high = np.percentile(means, [2.5, 97.5])
    observed = float(values.mean())
    p = 2.0 * float((means <= 0).mean() if observed >= 0 else (means >= 0).mean())
    return (float(low), float(high), min(1.0, p))


def economic_significance(
    per_event: pd.DataFrame,
    window: int,
    *,
    sessions: pd.DatetimeIndex,
    threshold: float = MIN_TRADEABLE_EDGE,
    block_sessions: int = 60,
    draws: int = 1000,
    seed: int = 20260813,
) -> dict:
    """單尾檢定 ``H0: alpha <= threshold``（M5）。

    為什麼要換掉 ``H0: alpha = 0``：一個統計上鐵證如山的 +0.4% 超額報酬，
    在 1.185% 的換手成本面前仍然是賠錢的。對「要不要投入資金」這個決策而言，
    對零檢定回答的是錯的問題。

    回傳的 ``p_value`` 是「觀察到的平均並未超過門檻」的證據強度：
    p 小＝有證據認為 edge 真的高於門檻。
    """
    column = f"car_{window}"
    frame = per_event.dropna(subset=[column])
    if frame.empty:
        return {"window": int(window), "observed": float("nan"),
                "threshold": float(threshold), "p_value": float("nan"),
                "exceeds_threshold": False, "events": 0}

    calendar = pd.Series(range(len(sessions)), index=pd.DatetimeIndex(sessions))
    position = calendar.reindex(pd.DatetimeIndex(frame["event_date"]))
    if position.isna().any():
        raise ValueError("事件日不在交易日曆內，無法分塊")

    block_id = position.to_numpy().astype(int) // block_sessions
    grouped = pd.Series(frame[column].to_numpy()).groupby(block_id)
    blocks = [g.to_numpy() for _, g in grouped]

    rng = np.random.default_rng(seed)
    n_blocks = len(blocks)
    means = np.empty(draws, dtype=float)
    for i in range(draws):
        picked = rng.integers(0, n_blocks, size=n_blocks)
        means[i] = np.concatenate([blocks[j] for j in picked]).mean()

    observed = float(frame[column].mean())
    return {
        "window": int(window),
        "events": int(len(frame)),
        "observed": observed,
        "threshold": float(threshold),
        # 單尾：重抽分布中落在門檻以下的比例
        "p_value": float((means <= threshold).mean()),
        "exceeds_threshold": bool(float(np.percentile(means, 5)) > threshold),
    }


def multiple_testing_budget(cells: int, significant: int,
                            *, alpha: float = 0.05) -> dict:
    """把「測了幾格」變成明確的帳（M2）。

    ``cells`` 是本輪實際計算並檢視的格數（因子 × 觀察窗），不是回報的格數。
    看過但沒寫進報告的格子一樣要算——那正是 data snooping 的定義。
    """
    if cells <= 0:
        raise ValueError("cells 必須為正；測了幾格就要誠實填幾格")
    expected = cells * alpha
    return {
        "cells": int(cells),
        "alpha": float(alpha),
        "significant_observed": int(significant),
        "expected_false_positives": float(expected),
        # Šidák 比 Bonferroni 精確一點，且在格子高度相關時仍偏保守
        "sidak_alpha": float(1.0 - (1.0 - alpha) ** (1.0 / cells)),
        "bonferroni_alpha": float(alpha / cells),
        # 顯著格數不超過期望誤報數 → 這批「顯著」與純雜訊無法區分
        "indistinguishable_from_noise": bool(significant <= expected),
    }


def label_verdict(*, p_value: float, observed: float,
                  threshold: float = MIN_TRADEABLE_EDGE,
                  alpha: float = 0.05) -> str:
    """三分類判決標籤（M6）。

    刻意**不提供**「有效」這個標籤。污染資料只能否證不能確認
    （`research/HYPOTHESES.md` H04~H06），而乾淨資料的確認要走
    `docs/STRATEGY_VALIDATION_PROTOCOL.md` 的完整流程，不是一個函式說了算。

    - ``rejected``：點估計連經濟門檻都沒到，且方向不利 → 可以正式除役
    - ``suggestive``：點估計超過門檻但統計上分不出來 → 待更多資料，不得部署
    - ``not_falsified``：超過門檻且顯著 → 只能宣稱「未被否證」
    """
    if not np.isfinite(p_value) or not np.isfinite(observed):
        return "undetermined"
    if observed < threshold:
        return "rejected"
    return "not_falsified" if p_value < alpha else "suggestive"
