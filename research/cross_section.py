"""Phase 3 橫斷面排序檢定（`docs/RESEARCH_V2_PIPELINE.md` §2 Phase 3）。

回答的問題不是「這個事件平均會不會漲」（那是 Phase 2），而是
**「同一天觸發的候選之間，能不能事前分辨出哪幾檔比較好」**。

三條紀律寫進實作，不留繞過的餘地：

1. **因子值一律取同一事件日內的橫斷面百分位**，不用原始值——
   原始值會讓極端值支配總分（量比 5.8 對 1.2 會直接輾壓其他因子）。
2. **先單因子，後組合**；組合一律等權 rank，本模組**不提供權重參數**，
   因為留下權重就等於留下優化的誘惑。
3. **單調性比 ICIR 大小重要**。單調關係比較像真實市場結構，
   而「加了它 ICIR 從 0.02 變 0.04」很可能只是雜訊。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_QUANTILES = 5
MIN_CANDIDATES_PER_DATE = 5


def cross_sectional_rank(frame: pd.DataFrame, factor: str,
                         *, date_column: str = "event_date") -> pd.Series:
    """同一事件日內的百分位（0~1，越大越高）。

    候選數不足 ``MIN_CANDIDATES_PER_DATE`` 的日期回傳 NaN——
    兩三檔的「百分位」沒有意義，硬算只會製造雜訊。
    """
    grouped = frame.groupby(date_column)[factor]
    ranks = grouped.rank(pct=True, method="average")
    counts = grouped.transform("count")
    return ranks.where(counts >= MIN_CANDIDATES_PER_DATE)


def quantile_profile(frame: pd.DataFrame, factor_rank: str, target: str,
                     *, quantiles: int = DEFAULT_QUANTILES) -> pd.DataFrame:
    """依因子百分位分組，回傳各組的目標平均值與樣本數。"""
    data = frame[[factor_rank, target]].dropna()
    if data.empty:
        return pd.DataFrame(columns=["quantile", "mean", "median", "count"])
    edges = np.linspace(0, 1, quantiles + 1)
    bucket = pd.cut(data[factor_rank], bins=edges, labels=False,
                    include_lowest=True, duplicates="drop")
    grouped = data.groupby(bucket)[target]
    return pd.DataFrame({
        "quantile": [int(q) + 1 for q in grouped.groups],
        "mean": grouped.mean().to_numpy(),
        "median": grouped.median().to_numpy(),
        "count": grouped.size().to_numpy(),
    })


def monotonicity(profile: pd.DataFrame) -> dict:
    """量化單調性：Spearman(分位序號, 平均值) 與端點價差。"""
    if len(profile) < 3:
        return {"spearman": float("nan"), "top_minus_bottom": float("nan"),
                "strictly_monotonic": False}
    values = profile["mean"].to_numpy()
    order = profile["quantile"].to_numpy()
    spearman = float(pd.Series(values).corr(pd.Series(order), method="spearman"))
    diffs = np.diff(values)
    return {
        "spearman": spearman,
        "top_minus_bottom": float(values[-1] - values[0]),
        "strictly_monotonic": bool(np.all(diffs > 0) or np.all(diffs < 0)),
    }


def rank_ic(frame: pd.DataFrame, factor: str, target: str,
            *, date_column: str = "event_date") -> dict:
    """逐日 Spearman(因子, 目標)，回傳 mean IC / IC std / ICIR。

    以**事件日**為單位計算後再平均，而不是把所有事件混在一起算一個相關係數
    ——後者會讓事件多的日子支配結果。
    """
    per_date = []
    for _, group in frame.groupby(date_column):
        pair = group[[factor, target]].dropna()
        if len(pair) < MIN_CANDIDATES_PER_DATE:
            continue
        value = pair[factor].corr(pair[target], method="spearman")
        if np.isfinite(value):
            per_date.append(value)
    if not per_date:
        return {"dates": 0, "mean_ic": float("nan"),
                "ic_std": float("nan"), "icir": float("nan")}
    series = pd.Series(per_date, dtype=float)
    std = float(series.std(ddof=1))
    return {
        "dates": int(len(series)),
        "mean_ic": float(series.mean()),
        "ic_std": std,
        "icir": float(series.mean() / std) if std else float("nan"),
    }


def equal_weight_score(frame: pd.DataFrame, factor_ranks: list[str]) -> pd.Series:
    """等權合成分數＝各因子百分位的平均。

    **刻意不提供權重參數。** 專案已經有一次「0.23/0.17/0.31/0.29」式的教訓，
    那些小數點幾乎必然含大量雜訊；留下介面就等於留下優化的誘惑。
    缺值因子不參與平均，但至少要有一半因子有值才給分。
    """
    available = frame[factor_ranks]
    enough = available.notna().sum(axis=1) >= max(1, len(factor_ranks) // 2)
    return available.mean(axis=1, skipna=True).where(enough)


def top_k_selection_return(frame: pd.DataFrame, score: str, target: str,
                           *, k: int = 10, date_column: str = "event_date") -> pd.Series:
    """每個事件日依分數取前 k 檔，回傳逐日的平均目標值。

    這模擬「每天 20 檔候選只能買 10 檔」的實際約束。
    """
    picks = []
    for date, group in frame.groupby(date_column):
        usable = group[[score, target]].dropna()
        if usable.empty:
            continue
        chosen = usable.nlargest(min(k, len(usable)), score)
        picks.append((date, float(chosen[target].mean())))
    if not picks:
        return pd.Series(dtype=float)
    return pd.Series(dict(picks)).sort_index()


def random_selection_baseline(frame: pd.DataFrame, target: str, *, k: int = 10,
                              draws: int = 1000, seed: int = 20260813,
                              date_column: str = "event_date") -> dict:
    """隨機挑 k 檔的 Monte Carlo 基準。

    這組基準直接回答「布林訊號本身到底是策略還是只是 candidate generator」：
    若完整排序打不贏隨機挑，那排序沒有資訊量。
    """
    rng = np.random.default_rng(seed)
    groups = [g[target].dropna().to_numpy()
              for _, g in frame.groupby(date_column)]
    groups = [g for g in groups if len(g)]
    if not groups:
        return {"draws": 0, "mean": float("nan"), "p05": float("nan"), "p95": float("nan")}
    outcomes = np.empty(draws, dtype=float)
    for i in range(draws):
        daily = [rng.choice(g, size=min(k, len(g)), replace=False).mean() for g in groups]
        outcomes[i] = float(np.mean(daily))
    return {
        "draws": draws,
        "mean": float(outcomes.mean()),
        "p05": float(np.percentile(outcomes, 5)),
        "p95": float(np.percentile(outcomes, 95)),
    }
