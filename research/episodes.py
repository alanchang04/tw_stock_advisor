"""事件叢集去重與右尾稽核（`docs/RESEARCH_METHOD_AMENDMENTS.md` M3／M4）。

M3 — 同一段行情被重複計算
--------------------------
一檔股票在 1/2、1/15、2/3、2/18 各觸發一次訊號，接著漲了 +100%。
若四筆都當成獨立事件，那**同一段 +100% 的行情就被計入四次**。
「前 5% 交易貢獻總和 438%」這種數字，很可能有相當部分是這樣來的。

修正方式是 `deduplicate_overlapping_events`：同一檔股票在前一次事件的
觀察窗尚未結束前再度觸發，**不計為新的獨立事件**。這不是濾網——它不改變
事件定義，只是把「一次價格事件」還原成一筆觀測。

M4 — 平均為正、中位數為負時，要問的是右尾的廣度
------------------------------------------------
這種形態本身**不代表策略無效**：趨勢跟隨與創投都是這個形狀，
勝率低但期望值為正是可以成立的。真正該問的不是「平均是不是正的」，而是
**右尾是可重複出現的現象，還是幾次歷史上的特例**。

`tail_breadth` 與 `leave_one_out_audit` 就是在量這件事：右尾集中在幾檔股票、
幾個年度？拿掉最好的那一年或那一檔，結論還在不在？
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def deduplicate_overlapping_events(
    events: pd.DataFrame,
    *,
    sessions: pd.DatetimeIndex,
    window_sessions: int,
    date_column: str = "event_date",
    stock_column: str = "stock_id",
) -> pd.DataFrame:
    """同一檔股票在觀察窗內重複觸發者只保留第一筆（M3）。

    貪婪由早到晚掃描：保留一筆之後，該股票在其後 ``window_sessions``
    個交易日內的觸發全部併入同一個 episode。這保證任兩筆保留下來的事件
    **觀察窗不重疊**，因此不會共用同一段價格走勢。

    回傳的欄位與輸入相同，另加：

    - ``episode_id``：``{stock_id}#{序號}``
    - ``absorbed``：這個 episode 吸收掉幾筆重複觸發（0 代表本來就是獨立的）
    """
    required = {date_column, stock_column}
    if not required <= set(events.columns):
        raise ValueError(f"events 需含 {sorted(required)}")
    if window_sessions <= 0:
        raise ValueError("window_sessions 必須為正")
    if events.empty:
        return events.assign(episode_id=pd.Series(dtype=str),
                             absorbed=pd.Series(dtype=int))

    calendar = pd.Series(range(len(sessions)), index=pd.DatetimeIndex(sessions))
    frame = events.copy()
    frame[date_column] = pd.to_datetime(frame[date_column])
    position = calendar.reindex(pd.DatetimeIndex(frame[date_column]))
    if position.isna().any():
        raise ValueError("事件日不在交易日曆內，無法判定窗是否重疊")
    frame["_pos"] = position.to_numpy().astype(int)
    frame = frame.sort_values([stock_column, "_pos"], kind="stable")

    keep_index, episode_ids, absorbed = [], [], []
    for stock, group in frame.groupby(stock_column, sort=False):
        last_kept = None
        counter = 0
        for index, pos in zip(group.index, group["_pos"]):
            if last_kept is None or pos - last_kept >= window_sessions:
                counter += 1
                last_kept = int(pos)
                keep_index.append(index)
                episode_ids.append(f"{stock}#{counter}")
                absorbed.append(0)
            else:
                absorbed[-1] += 1

    kept = frame.loc[keep_index].drop(columns="_pos")
    kept["episode_id"] = episode_ids
    kept["absorbed"] = absorbed
    return kept.sort_values([date_column, stock_column],
                            kind="stable").reset_index(drop=True)


def tail_breadth(per_event: pd.DataFrame, column: str, *,
                 top_fraction: float = 0.05,
                 stock_column: str = "stock_id",
                 date_column: str = "event_date") -> dict:
    """右尾的廣度：它是重複出現的現象，還是幾次特例？（M4）

    ``distinct_stock_ratio`` 是關鍵。若右尾的 500 筆來自 480 檔不同股票，
    那是一個**廣泛存在**的現象；若來自 40 檔，那多半是少數個股的大行情
    被重複計算或集中在特定族群。

    ``top_year_share`` 回答的是同一件事的時間版本：右尾若有六成落在同一年，
    那個「策略」其實是在描述那一年的市況。
    """
    data = per_event.dropna(subset=[column])
    if data.empty:
        return {"tail_events": 0}
    k = max(1, int(round(len(data) * top_fraction)))
    tail = data.nlargest(k, column)

    total = float(data[column].sum())
    tail_sum = float(tail[column].sum())
    stock_counts = tail[stock_column].value_counts()
    years = pd.DatetimeIndex(tail[date_column]).year.value_counts()

    return {
        "events": int(len(data)),
        "tail_events": int(len(tail)),
        "top_fraction": float(top_fraction),
        # 總和的多少比例來自右尾。>1.0 代表拿掉右尾之後總和為負。
        "tail_share_of_sum": float(tail_sum / total) if total else float("nan"),
        "distinct_stocks": int(stock_counts.size),
        # 1.0 = 右尾每一筆都是不同股票；越低代表越集中在少數個股
        "distinct_stock_ratio": float(stock_counts.size / len(tail)),
        "largest_stock_share": float(stock_counts.iloc[0] / len(tail)),
        # 股票層級的 Herfindahl；1.0 = 全部來自同一檔
        "stock_hhi": float(((stock_counts / len(tail)) ** 2).sum()),
        "distinct_years": int(years.size),
        "top_year": int(years.index[0]),
        "top_year_share": float(years.iloc[0] / len(tail)),
    }


def leave_one_out_audit(per_event: pd.DataFrame, column: str, *,
                        by: str, date_column: str = "event_date") -> dict:
    """逐一拿掉一個分組後重算平均，找出結論最依賴哪一組（M4）。

    ``by`` 可為 ``"year"``（用 ``date_column`` 推出年度）或任一欄位名
    （例如 ``stock_id``、``episode_id``）。

    ``flips_sign_groups`` 是最重要的一個數字：**只要有任何一組拿掉後平均變號，
    這個結論就是被那一組撐起來的**，不能宣稱它是普遍現象。
    """
    data = per_event.dropna(subset=[column]).copy()
    if data.empty:
        return {"groups": 0}
    if by == "year":
        data["_group"] = pd.DatetimeIndex(data[date_column]).year
    elif by in data.columns:
        data["_group"] = data[by]
    else:
        raise ValueError(f"未知的分組依據：{by}")

    values = data[column].to_numpy(dtype=float)
    total, n = float(values.sum()), len(values)
    full_mean = total / n

    results = {}
    for group, chunk in data.groupby("_group", sort=False):
        remaining = n - len(chunk)
        if remaining <= 0:
            continue
        results[group] = (total - float(chunk[column].sum())) / remaining
    if not results:
        return {"groups": 0}

    series = pd.Series(results, dtype=float)
    worst = series.idxmin() if full_mean >= 0 else series.idxmax()
    flips = int(((series < 0) != (full_mean < 0)).sum())
    return {
        "by": by,
        "groups": int(series.size),
        "full_mean": full_mean,
        "most_influential_group": (worst.item() if hasattr(worst, "item") else worst),
        "mean_without_it": float(series[worst]),
        "worst_case_drop": float(full_mean - series[worst]),
        "flips_sign_groups": flips,
        "conclusion_survives_any_single_removal": bool(flips == 0),
    }
