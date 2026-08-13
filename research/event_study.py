"""Phase 2 事件研究引擎（`docs/RESEARCH_V2_PIPELINE.md` §2 Phase 2）。

只回答一件事：**這個事件之後，相對大盤會不會漲？**

因此本模組**刻意不含**停損、trailing、部位大小、排序權重、產業上限、
換手成本。那些屬 Phase 3~5；現在放進來就會重蹈「進出場混在一起優化」的覆轍。

三個修正舊 P1 研究缺陷的地方
----------------------------
`SPEC_DATA_FOUNDATION_AND_MOMENTUM.md` §1.3 記載舊因子報告有兩項會**系統性
高估顯著性**的缺陷，本模組逐一修正：

1. **舊報告從事件日收盤計 forward return。** 但因子在收盤後才完整可知，
   那等於用當天收盤價成交。本模組一律 **自 t+1 開盤起算**。
2. **舊報告把重疊的 forward return 當獨立樣本算 t-stat。** 20/60/120 日窗
   彼此大量重疊，會把有效樣本數誇大數倍。本模組用 **block bootstrap**，
   block 長度由呼叫端事前固定。
3. 舊報告用原始報酬。本模組一律用 **超額報酬**（個股 − 同期基準）。

用途界線
--------
本模組讀 2015~2026 的營運快照，**不是** release `tw_stock_data_2005_2014_r2`，
因此不涉及該 release `usage_policy` 對 2008~2014 holdout 的禁令。

但依 `research/HYPOTHESES.md` H04~H06，2015~2026 對那些假說已污染，
**結果只能否證、不能確認**：效果消失＝真實負面結果；效果存活只能宣稱
「未被否證」。呼叫端必須在報告中標明這一點。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

DEFAULT_WINDOWS = (5, 10, 20, 40, 60)
DEFAULT_BLOCK_SESSIONS = 20


@dataclass(frozen=True)
class EventStudyResult:
    windows: tuple[int, ...]
    events: int
    distinct_event_dates: int
    stocks: int
    car_mean: dict[int, float]
    car_median: dict[int, float]
    ci_low: dict[int, float]
    ci_high: dict[int, float]
    p_value: dict[int, float]
    block_sessions: int
    bootstrap_draws: int
    per_event: pd.DataFrame = field(repr=False, default_factory=pd.DataFrame)

    def is_monotonic_path(self) -> bool:
        """CAR 是否隨窗長單調不減——單調遞增不回吐與先漲後吐是兩件事。"""
        values = [self.car_mean[w] for w in self.windows]
        return all(b >= a for a, b in zip(values, values[1:]))


def forward_excess_returns(
    *,
    events: pd.DataFrame,
    open_prices: pd.DataFrame,
    benchmark_nav: pd.Series,
    windows=DEFAULT_WINDOWS,
) -> pd.DataFrame:
    """逐事件計算各窗長的超額報酬。

    ``events`` 需含 ``stock_id`` 與 ``event_date``（訊號日）。
    **成交自 t+1 開盤起算**：事件日收盤後才知道訊號，因此進場價是下一個
    交易日的開盤價，出場價是再往後 h 個交易日的開盤價。

    基準為同期 ``benchmark_nav``（總報酬），以相同的 t+1 對齊，
    因此兩邊的時點完全一致，不會因為對齊方式不同而製造假的超額。
    """
    if not {"stock_id", "event_date"} <= set(events.columns):
        raise ValueError("events 需含 stock_id 與 event_date")
    sessions = pd.DatetimeIndex(open_prices.index)
    if not sessions.is_monotonic_increasing:
        raise ValueError("open_prices 必須按交易日升冪排序")

    benchmark = benchmark_nav.reindex(sessions).astype(float)
    rows = []
    for stock_id, event_date in zip(events["stock_id"].astype(str),
                                    pd.to_datetime(events["event_date"])):
        if stock_id not in open_prices.columns:
            continue
        # 嚴格晚於事件日的第一個交易日＝進場日
        entry_pos = sessions.searchsorted(event_date, side="right")
        if entry_pos >= len(sessions):
            continue
        entry_price = open_prices.iat[entry_pos, open_prices.columns.get_loc(stock_id)]
        entry_bench = benchmark.iat[entry_pos]
        if not np.isfinite(entry_price) or entry_price <= 0 or not np.isfinite(entry_bench):
            continue

        record = {"stock_id": stock_id, "event_date": event_date,
                  "entry_date": sessions[entry_pos]}
        usable = False
        for window in windows:
            exit_pos = entry_pos + window
            if exit_pos >= len(sessions):
                record[f"car_{window}"] = np.nan
                continue
            exit_price = open_prices.iat[exit_pos, open_prices.columns.get_loc(stock_id)]
            exit_bench = benchmark.iat[exit_pos]
            if not np.isfinite(exit_price) or exit_price <= 0 or not np.isfinite(exit_bench):
                record[f"car_{window}"] = np.nan
                continue
            stock_return = exit_price / entry_price - 1.0
            bench_return = exit_bench / entry_bench - 1.0
            record[f"car_{window}"] = stock_return - bench_return
            usable = True
        if usable:
            rows.append(record)
    return pd.DataFrame(rows)


def block_bootstrap_ci(
    per_event: pd.DataFrame,
    window: int,
    *,
    sessions: pd.DatetimeIndex,
    block_sessions: int = DEFAULT_BLOCK_SESSIONS,
    draws: int = 1000,
    seed: int = 20260813,
) -> tuple[float, float, float]:
    """以事件日的時間 block 重抽，回傳 (ci_low, ci_high, p_value)。

    為什麼要 block 而不是逐事件重抽：同一段期間的事件高度相關
    （一次市場性行情同時觸發數十檔），逐事件重抽會把有效樣本數誇大。
    block 以**事件日**分組後整段抽取，保留同期事件的相關性。

    ``p_value`` 為雙尾，檢定「平均超額報酬為 0」。
    """
    column = f"car_{window}"
    frame = per_event.dropna(subset=[column])
    if frame.empty:
        return (np.nan, np.nan, np.nan)

    # 以**完整交易日曆**的序位分塊。兩種錯誤寫法都試過並否決：
    #   日曆天數 → block 比宣稱的短約三成，檢定過寬；
    #   相異事件日序位 → 月頻事件的 20 個相異日等於 20 個「月」，block 過長。
    # 只有對齊真實交易日曆，block_sessions 才真的是「幾個交易日」。
    dates = pd.DatetimeIndex(frame["event_date"])
    calendar = pd.Series(range(len(sessions)), index=pd.DatetimeIndex(sessions))
    position = calendar.reindex(dates)
    if position.isna().any():
        raise ValueError("事件日不在交易日曆內，無法分塊")
    block_id = (position.to_numpy().astype(int) // block_sessions)
    groups = pd.Series(frame[column].to_numpy()).groupby(block_id)
    block_values = [g.to_numpy() for _, g in groups]
    if not block_values:
        return (np.nan, np.nan, np.nan)

    rng = np.random.default_rng(seed)
    n_blocks = len(block_values)
    means = np.empty(draws, dtype=float)
    for i in range(draws):
        picked = rng.integers(0, n_blocks, size=n_blocks)
        sample = np.concatenate([block_values[j] for j in picked])
        means[i] = sample.mean()
    low, high = np.percentile(means, [2.5, 97.5])
    # 雙尾 p：重抽分布中落在 0 另一側的比例
    observed = frame[column].mean()
    if observed >= 0:
        p = 2.0 * float((means <= 0).mean())
    else:
        p = 2.0 * float((means >= 0).mean())
    return (float(low), float(high), min(1.0, p))


def run_event_study(
    *,
    events: pd.DataFrame,
    open_prices: pd.DataFrame,
    benchmark_nav: pd.Series,
    windows=DEFAULT_WINDOWS,
    block_sessions: int = DEFAULT_BLOCK_SESSIONS,
    draws: int = 1000,
    seed: int = 20260813,
) -> EventStudyResult:
    """完整 Phase 2 檢定。所有窗長都會計算並回報，不得只挑好看的。"""
    per_event = forward_excess_returns(
        events=events, open_prices=open_prices,
        benchmark_nav=benchmark_nav, windows=windows,
    )
    car_mean, car_median, ci_low, ci_high, p_value = {}, {}, {}, {}, {}
    for window in windows:
        column = f"car_{window}"
        series = per_event[column].dropna() if column in per_event else pd.Series(dtype=float)
        car_mean[window] = float(series.mean()) if len(series) else float("nan")
        car_median[window] = float(series.median()) if len(series) else float("nan")
        low, high, p = block_bootstrap_ci(
            per_event, window, sessions=pd.DatetimeIndex(open_prices.index),
            block_sessions=block_sessions, draws=draws, seed=seed,
        ) if len(series) else (float("nan"),) * 3
        ci_low[window], ci_high[window], p_value[window] = low, high, p

    return EventStudyResult(
        windows=tuple(windows),
        events=int(len(per_event)),
        distinct_event_dates=int(per_event["event_date"].nunique()) if len(per_event) else 0,
        stocks=int(per_event["stock_id"].nunique()) if len(per_event) else 0,
        car_mean=car_mean, car_median=car_median,
        ci_low=ci_low, ci_high=ci_high, p_value=p_value,
        block_sessions=block_sessions, bootstrap_draws=draws,
        per_event=per_event,
    )
