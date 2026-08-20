"""H01 布林二次突破的事件偵測（規格：`docs/SPEC_H01_BOLLINGER_REBREAKOUT.md`）。

規格 §3 的狀態機：

    flat  --(收盤>上沿 且 帶寬擴張 且 量能確認)-->  B1，engaged
    engaged --(收盤回到帶內)-->  retreated
    retreated --(收盤>上沿)-->  **B2＝受測事件**，回到 flat
    retreated --(距 B1 超過 60 期)-->  作廢，回到 flat

**受測事件是 B2，不是 B1。** B1 只是進入狀態的條件。

60 期上限的理由（§3.1）：不設上限時 B1→B2 的循環長度 p95 為 100 個交易日，
第一次突破後 100 天才二次突破已經不是同一個 setup，而是另一個 regime。
這個上限事前選定，**不得事後掃描**。

參數全部由規格凍結，本模組不提供調整介面——留下可調參數就等於留下掃描的誘惑。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

BB_WINDOW = 20
BB_SIGMA = 2.0
BANDWIDTH_LOOKBACK = 60
VOLUME_WINDOW = 20
MAX_PERIODS_BETWEEN_BREAKS = 60


def bollinger_bands(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """BB(20, 2σ)。回傳 (中軌, 上沿, 下沿)。"""
    middle = close.rolling(BB_WINDOW).mean()
    deviation = close.rolling(BB_WINDOW).std(ddof=0)
    return middle, middle + BB_SIGMA * deviation, middle - BB_SIGMA * deviation


def setup_conditions(close: pd.Series, volume: pd.Series) -> tuple[pd.Series, pd.Series]:
    """回傳 (帶寬擴張, 量能確認)。"""
    middle, upper, lower = bollinger_bands(close)
    bandwidth = (upper - lower) / middle
    expanded = bandwidth > bandwidth.rolling(BANDWIDTH_LOOKBACK).median()
    volume_ok = volume >= volume.rolling(VOLUME_WINDOW).mean()
    return expanded.fillna(False), volume_ok.fillna(False)


def detect_rebreakouts(close: pd.Series, volume: pd.Series, *, side: str) -> pd.DataFrame:
    """逐檔偵測 B2 事件。``side`` 為 ``"upper"``（多）或 ``"lower"``（空）。

    回傳欄位：``first_break_date``、``event_date``（＝B2）、``periods_between``。
    """
    if side not in {"upper", "lower"}:
        raise ValueError("side 必須是 upper 或 lower")
    close = close.dropna()
    if len(close) < BANDWIDTH_LOOKBACK + BB_WINDOW:
        return pd.DataFrame(columns=["first_break_date", "event_date", "periods_between"])

    volume = volume.reindex(close.index)
    middle, upper, lower = bollinger_bands(close)
    outside = close > upper if side == "upper" else close < lower
    expanded, volume_ok = setup_conditions(close, volume)

    records = []
    state = "flat"
    first_index = -1
    values_outside = outside.to_numpy()
    values_expanded = expanded.to_numpy()
    values_volume = volume_ok.to_numpy()
    band = (upper if side == "upper" else lower).to_numpy()

    for i in range(len(close)):
        if not np.isfinite(band[i]):
            continue
        is_outside = bool(values_outside[i])
        if state == "flat":
            if is_outside and values_expanded[i] and values_volume[i]:
                first_index, state = i, "engaged"
        elif state == "engaged":
            if not is_outside:
                state = "retreated"
            elif i - first_index > MAX_PERIODS_BETWEEN_BREAKS:
                state = "flat"          # 一直在帶外，從未回縮：不是本假說的型態
        elif state == "retreated":
            if i - first_index > MAX_PERIODS_BETWEEN_BREAKS:
                state = "flat"          # §3.1 作廢
            elif is_outside:
                records.append({
                    "first_break_date": close.index[first_index],
                    "event_date": close.index[i],
                    "periods_between": i - first_index,
                })
                state = "flat"
    return pd.DataFrame(records)


def detect_all(close: pd.DataFrame, volume: pd.DataFrame, *, side: str) -> pd.DataFrame:
    """全市場版本。回傳含 ``stock_id`` 的事件表。"""
    frames = []
    for stock_id in close.columns:
        series = close[stock_id]
        events = detect_rebreakouts(series, volume[stock_id], side=side)
        if not events.empty:
            frames.append(events.assign(stock_id=str(stock_id)))
    if not frames:
        return pd.DataFrame(columns=["stock_id", "first_break_date",
                                     "event_date", "periods_between"])
    return pd.concat(frames, ignore_index=True)


def weekly_regime(close: pd.DataFrame) -> pd.DataFrame:
    """週 K 的多頭 regime：週收盤 > 週 MA20（規格 §3.2，只定方向不進出場）。

    回傳以**日**為 index 的布林矩陣，值為「該日所屬週的上一週狀態」——
    用上一週是為了避免用到當週尚未結束的資訊。
    """
    weekly = close.resample("W-FRI").last()
    regime = (weekly > weekly.rolling(BB_WINDOW).mean()).astype(bool)
    aligned = regime.shift(1).reindex(close.index, method="ffill")
    return aligned.fillna(False).astype(bool)
