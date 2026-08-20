"""可執行策略（policy）層：landmark 對齊、等待型進場、時機指標。

這個模組存在的理由是**兩個在 H12／H13 登記時被留下的前視陷阱**，
它們都不是「注意一點就好」，而是需要結構上做不到才安全。

M15 — 延遲資訊偏誤（H12）
--------------------------
「營收公布後 20 個交易日內投信累計買超 100 張」這個條件，
**在營收公布當日並不知道**。若一邊用那 20 天決定 `Confirm`、
一邊又把那 20 天的報酬算進績效，就是前視。

解法是 landmark design：決策時點推到確認窗結束之後，
前瞻報酬從決策時點之後才起算。`landmark_confirmation` 只回傳
決策日與確認旗標，且 `assert_information_precedes_decision` 會擋下
任何「資訊尚未確定就開始計酬」的組合。

M16 — 未來事件條件化（H13）
----------------------------
「只取後來有出現 B1 的股票來比較」與先前那個把效果放大 8 倍的
「只取後來有 B2 的 B1」是同一個錯誤。

解法是比較**完整可執行的 policy**：`wait_for_trigger_entry` 從決策日
往後最多等 `max_wait_sessions`，等到就進場、等不到就依事前規則不進場，
而且**沒等到的那些列會留在結果裡**（`entered=False`），不會從樣本消失。

M17 — 時機訊號的曝險幻覺
-------------------------
等越久、跳過越多股票，MAE 天生越好看；極端情況「永遠不買」的
MAE 是完美的 0%。因此 `exposure_summary` 與 MAE 必須一起報，
`participation_rate`、`mean_wait_sessions`、`missed_return` 缺一不可。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.inference import ROUND_TRIP_COST


def landmark_confirmation(
    events: pd.DataFrame,
    daily_flag: pd.DataFrame,
    *,
    sessions: pd.DatetimeIndex,
    window_sessions: int,
    threshold: float = 0.0,
    date_column: str = "event_date",
    stock_column: str = "stock_id",
) -> pd.DataFrame:
    """把「事件後 N 日內是否被確認」轉成一個 landmark 決策列（M15）。

    ``daily_flag`` 是逐日的確認強度（例如投信買超張數），在
    ``(event_date, event_date + window_sessions]`` 區間內加總後與
    ``threshold`` 比較。

    回傳欄位：

    - ``information_end``：確認窗的最後一個交易日 —— 資訊在此刻才完整
    - ``decision_date``：等同 ``information_end``，決策在收盤後做
    - ``confirmed``：布林
    - ``confirmation_value``：窗內加總值，供分位分析用

    **前瞻報酬必須自 ``decision_date`` 之後起算。** 呼叫端一律要再經過
    ``assert_information_precedes_decision`` 驗證，不得自行對齊。
    """
    if window_sessions <= 0:
        raise ValueError("window_sessions 必須為正；landmark 設計不容許零窗")
    required = {date_column, stock_column}
    if not required <= set(events.columns):
        raise ValueError(f"events 需含 {sorted(required)}")
    if events.empty:
        return events.assign(information_end=pd.NaT, decision_date=pd.NaT,
                             confirmed=False, confirmation_value=np.nan)

    calendar = pd.Series(range(len(sessions)), index=pd.DatetimeIndex(sessions))
    frame = events.copy()
    frame[date_column] = pd.to_datetime(frame[date_column])
    position = calendar.reindex(pd.DatetimeIndex(frame[date_column]))
    if position.isna().any():
        raise ValueError("事件日不在交易日曆內，無法定位確認窗")

    flags = daily_flag.reindex(index=sessions)
    values, ends = [], []
    for stock_id, start in zip(frame[stock_column].astype(str),
                               position.to_numpy().astype(int)):
        end = start + window_sessions
        if stock_id not in flags.columns or end >= len(sessions):
            values.append(np.nan)
            ends.append(pd.NaT)
            continue
        # 區間 (start, end]：不含事件日本身，事件日的活動屬於事件前的資訊
        window = flags.iloc[start + 1:end + 1][stock_id]
        values.append(float(window.sum(skipna=True)))
        ends.append(sessions[end])

    frame["confirmation_value"] = values
    frame["information_end"] = pd.DatetimeIndex(ends)
    frame["decision_date"] = frame["information_end"]
    frame["confirmed"] = frame["confirmation_value"] > threshold
    return frame.reset_index(drop=True)


def assert_information_precedes_decision(
    frame: pd.DataFrame,
    *,
    information_end: str = "information_end",
    forward_start: str = "forward_start",
) -> None:
    """守門：前瞻報酬的起算日必須**嚴格晚於**確認資訊的最後一日（M15）。

    這是 H12 唯一真正危險的地方。若允許 ``forward_start <= information_end``，
    等於一邊用那段報酬決定分組、一邊把它算進績效，結果必然虛高。
    """
    for column in (information_end, forward_start):
        if column not in frame.columns:
            raise ValueError(f"缺少欄位 {column}，無法驗證資訊時序")
    usable = frame[[information_end, forward_start]].dropna()
    if usable.empty:
        return
    offending = usable[pd.to_datetime(usable[forward_start])
                       <= pd.to_datetime(usable[information_end])]
    if not offending.empty:
        raise ValueError(
            f"{len(offending)} 列的前瞻報酬起算日不晚於確認資訊結束日；"
            "這是延遲資訊前視（M15），不得繼續計算")


def wait_for_trigger_entry(
    candidates: pd.DataFrame,
    trigger: pd.DataFrame,
    *,
    sessions: pd.DatetimeIndex,
    max_wait_sessions: int,
    decision_column: str = "decision_date",
    stock_column: str = "stock_id",
) -> pd.DataFrame:
    """等待型進場 policy：等到觸發就買，等不到就不買（M16）。

    **沒等到觸發的候選會保留在回傳結果中**（``entered=False``），
    這正是本函式與「只取後來有觸發的樣本」的差別——後者是用未來事件
    篩選樣本，先前已經在 B1／B2 上把效果放大過約 8 倍。

    ``max_wait_sessions`` 必須事前固定；等待上限是 policy 的一部分，
    不是可以事後調的參數。
    """
    if max_wait_sessions <= 0:
        raise ValueError("max_wait_sessions 必須為正")
    required = {decision_column, stock_column}
    if not required <= set(candidates.columns):
        raise ValueError(f"candidates 需含 {sorted(required)}")
    if candidates.empty:
        return candidates.assign(entry_date=pd.NaT, wait_sessions=np.nan,
                                 entered=False)

    calendar = pd.Series(range(len(sessions)), index=pd.DatetimeIndex(sessions))
    frame = candidates.copy()
    frame[decision_column] = pd.to_datetime(frame[decision_column])
    flags = trigger.reindex(index=sessions).fillna(False).astype(bool)

    entry_dates, waits, entered = [], [], []
    for stock_id, decision in zip(frame[stock_column].astype(str),
                                  frame[decision_column]):
        start = calendar.get(decision, None)
        if start is None or pd.isna(decision) or stock_id not in flags.columns:
            entry_dates.append(pd.NaT)
            waits.append(np.nan)
            entered.append(False)
            continue
        start = int(start)
        # 觀察窗 (decision, decision + max_wait]；成交在觸發日的下一個交易日
        window = flags.iloc[start + 1:start + 1 + max_wait_sessions][stock_id]
        hits = np.flatnonzero(window.to_numpy())
        if hits.size == 0 or start + 1 + int(hits[0]) + 1 >= len(sessions):
            entry_dates.append(pd.NaT)
            waits.append(np.nan)
            entered.append(False)
            continue
        trigger_pos = start + 1 + int(hits[0])
        entry_dates.append(sessions[trigger_pos + 1])
        waits.append(float(trigger_pos - start))
        entered.append(True)

    frame["entry_date"] = pd.DatetimeIndex(entry_dates)
    frame["wait_sessions"] = waits
    frame["entered"] = entered
    return frame.reset_index(drop=True)


def exposure_summary(policy: pd.DataFrame, *, entered: str = "entered",
                     wait: str = "wait_sessions") -> dict:
    """曝險統計。**必須與 MAE 並列報告**（M17）。

    沒有這組數字時，「永遠不買」會是 MAE 最好的策略。
    """
    total = int(len(policy))
    if total == 0:
        return {"candidates": 0, "entered": 0, "participation_rate": float("nan"),
                "mean_wait_sessions": float("nan"),
                "median_wait_sessions": float("nan")}
    taken = policy[policy[entered].astype(bool)]
    waits = taken[wait].dropna() if wait in taken else pd.Series(dtype=float)
    return {
        "candidates": total,
        "entered": int(len(taken)),
        "participation_rate": float(len(taken) / total),
        "mean_wait_sessions": float(waits.mean()) if len(waits) else float("nan"),
        "median_wait_sessions": float(waits.median()) if len(waits) else float("nan"),
    }


def max_adverse_excursion(
    entries: pd.DataFrame,
    open_prices: pd.DataFrame,
    low_prices: pd.DataFrame,
    *,
    sessions: pd.DatetimeIndex,
    horizon: int,
    entry_column: str = "entry_date",
    stock_column: str = "stock_id",
) -> pd.Series:
    """每筆進場在 ``horizon`` 個交易日內的最大逆向波動（負值或 0）。

    用**原始**報酬而非超額報酬：MAE 的經濟意義是「離停損多近、有多痛」，
    而停損是看自己的價格觸發的，不是看相對大盤的落後幅度。
    兩組（有／無時機訊號）經歷同一段市場，市場成分在**組間差異**中大致抵銷。

    以盤中最低價計算，因為停損看的是盤中而不是收盤。
    """
    if horizon <= 0:
        raise ValueError("horizon 必須為正")
    calendar = pd.Series(range(len(sessions)), index=pd.DatetimeIndex(sessions))
    results = {}
    for index, stock_id, entry in zip(entries.index,
                                      entries[stock_column].astype(str),
                                      pd.to_datetime(entries[entry_column])):
        position = calendar.get(entry, None)
        if position is None or pd.isna(entry) or stock_id not in open_prices.columns:
            results[index] = np.nan
            continue
        position = int(position)
        entry_price = open_prices.iloc[position][stock_id]
        if not np.isfinite(entry_price) or entry_price <= 0:
            results[index] = np.nan
            continue
        path = low_prices.iloc[position:position + horizon][stock_id].dropna()
        if path.empty:
            results[index] = np.nan
            continue
        results[index] = float(path.min() / entry_price - 1.0)
    return pd.Series(results, dtype=float)


def time_to_breakeven(
    entries: pd.DataFrame,
    open_prices: pd.DataFrame,
    close_prices: pd.DataFrame,
    *,
    sessions: pd.DatetimeIndex,
    horizon: int,
    threshold: float = ROUND_TRIP_COST,
    entry_column: str = "entry_date",
    stock_column: str = "stock_id",
) -> pd.DataFrame:
    """首次達到「足以覆蓋完整換手成本」所需的交易日數，右設限於 ``horizon``。

    門檻用**完整換手成本**（預設 1.185%）而不是 +3%／+5%／+10% 這種
    任選數字——那三個是三個自由度，而成本是外生的、事前已知的。

    回傳 ``duration``（交易日）與 ``event``（1＝達標、0＝設限），
    可直接餵給存活分析。
    """
    if horizon <= 0:
        raise ValueError("horizon 必須為正")
    calendar = pd.Series(range(len(sessions)), index=pd.DatetimeIndex(sessions))
    rows = []
    for index, stock_id, entry in zip(entries.index,
                                      entries[stock_column].astype(str),
                                      pd.to_datetime(entries[entry_column])):
        position = calendar.get(entry, None)
        if position is None or pd.isna(entry) or stock_id not in open_prices.columns:
            rows.append({"duration": np.nan, "event": np.nan})
            continue
        position = int(position)
        entry_price = open_prices.iloc[position][stock_id]
        if not np.isfinite(entry_price) or entry_price <= 0:
            rows.append({"duration": np.nan, "event": np.nan})
            continue
        path = close_prices.iloc[position:position + horizon][stock_id]
        gains = (path / entry_price - 1.0).to_numpy()
        hits = np.flatnonzero(np.nan_to_num(gains, nan=-np.inf) >= threshold)
        if hits.size:
            rows.append({"duration": float(hits[0] + 1), "event": 1.0})
        else:
            rows.append({"duration": float(horizon), "event": 0.0})
    return pd.DataFrame(rows, index=entries.index)


def restricted_mean_time(durations: pd.DataFrame, *, horizon: int) -> float:
    """設限平均等待時間：在固定 ``horizon`` 內平均要等多久才回本。

    比 Cox 的 hazard ratio 適合這裡：時機訊號的效果很可能集中在前段
    （前 10 天差很多、之後沒差），那會直接違反 proportional hazards 假設。
    設限平均不需要把效果壓成一個固定的 hazard ratio。
    """
    usable = durations.dropna(subset=["duration"])
    if usable.empty:
        return float("nan")
    return float(np.minimum(usable["duration"].to_numpy(), horizon).mean())
