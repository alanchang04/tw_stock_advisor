"""月頻 Fama-MacBeth 與報酬反應路徑（`docs/RESEARCH_METHOD_AMENDMENTS.md` M7）。

為什麼要有這個模組
------------------
`research/HYPOTHESES.md` 曾寫下這句話：

> 月營收事件在 11.5 年只有 137 個相異事件日，120 日窗重疊 5/6，
> **有效獨立觀測僅約 23 個**。

這句話**只對了一半，而錯的那一半很重要**。它把「月營收因子有沒有選股資訊」
壓縮成「137 個月的 6 個月累積報酬平均是否為正」這一個時間序列問題，
於是丟掉了每個月數百到上千檔股票之間的**橫斷面資訊**。

正確的框架是逐月做橫斷面迴歸

    R[i, t+k] = a[t] + b[t] * X[i, t] + e[i, t+k]

得到係數序列 ``b[t]``，再對這 137 個係數做時間序列推論。橫斷面的寬度直接
決定每個 ``b[t]`` 的精度，這正是原本被丟掉的資訊。

重疊問題也一併解決，但要用對方法
--------------------------------
關鍵在 **左邊變數用「第 k 個月的單月報酬」，不是「k 個月的累積報酬」**。
``b[t]`` 用的是 t+k 月、``b[t+1]`` 用的是 t+1+k 月——彼此不重疊。
於是反應路徑的每一階都是乾淨的非重疊估計，重疊只在「把路徑加總成累積」
時才出現，而那個加總的變異數可以直接對加總後的序列做 Newey-West 求得。

這也給出一個比累積報酬有用得多的診斷：**alpha 是第幾個月出現的？**
逐月遞增像是資訊緩慢擴散；前五個月都是零、第六個月突然跳出來的，
幾乎可以確定是雜訊或某種對齊錯誤。

Newey-West 處理的是殘存的自相關（同一波市況跨越數月），
不是機械性重疊——那個已經在設計上消掉了。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# 反應路徑預設看 6 個月，對應現行策略持有期 p95 ≈ 110 個交易日
DEFAULT_HORIZONS = (1, 2, 3, 4, 5, 6)
# 最少要幾檔才值得跑一次橫斷面迴歸；太少的月份係數純粹是雜訊
MIN_CROSS_SECTION = 30


def build_forward_panel(*, signal: pd.DataFrame, decision_frame: pd.DataFrame,
                        forward_frame: pd.DataFrame,
                        controls: dict[str, pd.DataFrame] | None = None,
                        horizons=DEFAULT_HORIZONS) -> pd.DataFrame:
    """把寬表組成 (股票, 決策期) 的長表，含各領先期的**單期**報酬。

    兩個報酬矩陣是分開的參數，而且**必須**分開，這是本函式存在的理由：

    - ``decision_frame`` 決定誰進得了當期橫斷面。它可以套用合格條件，
      因為那是決策當下就知道的資訊。
    - ``forward_frame`` 提供前瞻報酬。它**不得**套用任何在決策之後才知道的
      條件——包括「該股在 m+3 月是否仍然合格」。

    把同一個已遮罩的矩陣同時傳給兩者，等於只保留「後來還活著且仍夠流動」
    的股票，那是存活者偏差。本專案實測過這個錯誤的量級：月營收因子
    6 個月累積由 **+2.02% 被高估成 +3.03%**，投信連買則由 +1.86% 被
    **低估成 -1.19%**（連符號都反了）——偏誤方向隨因子的規模結構而不同，
    所以不能用「反正都偏正」帶過。
    """
    controls = controls or {}
    frames = {"signal": signal.where(decision_frame.notna()).stack(future_stack=True)}
    for name, frame in controls.items():
        frames[name] = frame.stack(future_stack=True)
    for horizon in horizons:
        frames[f"fwd_{horizon}"] = forward_frame.shift(-horizon).stack(
            future_stack=True)
    panel = pd.DataFrame(frames).dropna(subset=["signal"])
    panel.index.names = ["period", "stock_id"]
    return panel.reset_index()


def newey_west_se(series: pd.Series, *, lags: int) -> float:
    """序列平均值的 Newey-West (Bartlett) 標準誤。

    ``lags`` 應依經濟意義事前選定，不得為了拿到想要的 p 值而調整。
    月頻資料檢定 k 個月的效果時，取 ``lags = k`` 是慣例的保守選擇。
    """
    values = pd.Series(series, dtype=float).dropna().to_numpy()
    n = values.size
    if n < 2:
        return float("nan")
    if lags < 0:
        raise ValueError("lags 不得為負")
    lags = min(lags, n - 1)

    centered = values - values.mean()
    variance = float(centered @ centered) / n
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1.0)
        covariance = float(centered[lag:] @ centered[:-lag]) / n
        variance += 2.0 * weight * covariance
    if variance <= 0:
        return float("nan")
    return float(np.sqrt(variance / n))


def _cross_sectional_slope(x: np.ndarray, y: np.ndarray,
                           controls: np.ndarray | None = None) -> float:
    """帶截距（可選帶控制變數）的 OLS，回傳**受測因子**的係數。

    沒有控制變數且因子為二元時，係數正好等於「有訊號組平均 − 無訊號組平均」，
    因此可以直接讀成超額報酬的差，不需要另外換算。

    加入控制變數後，係數變成「控制住那些變數之後」的差——這是回答
    「這個效果是不是只是小型股傾斜」的方式（M9）。
    """
    if x.size < MIN_CROSS_SECTION:
        return float("nan")
    if np.ptp(x) == 0:                       # 全部同值，斜率無定義
        return float("nan")
    blocks = [np.ones_like(x), x]
    if controls is not None and controls.size:
        blocks.append(controls)
    design = np.column_stack(blocks)
    if np.linalg.matrix_rank(design) < design.shape[1]:
        return float("nan")                  # 共線，係數無定義
    solution, *_ = np.linalg.lstsq(design, y, rcond=None)
    return float(solution[1])


def fama_macbeth(panel: pd.DataFrame, *, factor: str, target: str,
                 period: str = "period", lags: int = 6,
                 controls: list[str] | None = None) -> dict:
    """逐期橫斷面迴歸 → 係數序列 → Newey-West 推論。

    ``panel`` 每列是一個 (股票, 期別) 觀測，需含 ``period``／``factor``／``target``。
    回傳含 ``coefficients``（逐期係數序列），呼叫端可再組成累積路徑。

    **這個估計式對「共同基準」免疫。** 逐期迴歸的截距 ``a[m]`` 會吸收當期
    所有股票共有的成分，因此把左邊換成「減去 0050」或「減去等權 universe」
    得到的因子係數完全相同。基準選擇只影響**組合層級**的績效宣稱，
    不影響橫斷面選股資訊的估計——這一點值得記住，它省掉一整輪無謂的爭論。
    但截距吸收不了**規模或產業傾斜**，那要靠 ``controls`` 明確控制。
    """
    controls = list(controls or [])
    required = {period, factor, target, *controls}
    if not required <= set(panel.columns):
        raise ValueError(f"panel 需含 {sorted(required)}")

    data = panel[[period, factor, target, *controls]].dropna()
    coefficients = {}
    widths = {}
    for key, group in data.groupby(period, sort=True):
        slope = _cross_sectional_slope(
            group[factor].to_numpy(dtype=float),
            group[target].to_numpy(dtype=float),
            group[controls].to_numpy(dtype=float) if controls else None,
        )
        if np.isfinite(slope):
            coefficients[key] = slope
            widths[key] = int(len(group))

    if not coefficients:
        return {"periods": 0, "mean": float("nan"), "se": float("nan"),
                "t_stat": float("nan"), "p_value": float("nan"),
                "coefficients": pd.Series(dtype=float)}

    series = pd.Series(coefficients, dtype=float).sort_index()
    mean = float(series.mean())
    se = newey_west_se(series, lags=lags)
    t_stat = mean / se if se and np.isfinite(se) else float("nan")
    return {
        "periods": int(series.size),
        "median_cross_section": int(pd.Series(widths).median()),
        "mean": mean,
        "se": se,
        "t_stat": float(t_stat),
        "p_value": _two_sided_p(t_stat),
        "newey_west_lags": int(min(lags, series.size - 1)),
        "coefficients": series,
    }


def response_path(panel: pd.DataFrame, *, factor: str,
                  target_prefix: str = "fwd_", horizons=DEFAULT_HORIZONS,
                  period: str = "period", lags: int = 6,
                  controls: list[str] | None = None) -> pd.DataFrame:
    """各月度領先期的 Fama-MacBeth 係數，組成反應路徑。

    ``panel`` 需含 ``{target_prefix}{h}`` 欄，內容是**第 h 個月的單月報酬**
    （非累積）。用累積報酬會把重疊重新引入，使推論回到原本的困境。
    """
    rows = []
    for horizon in horizons:
        target = f"{target_prefix}{horizon}"
        if target not in panel.columns:
            raise ValueError(f"panel 缺少 {target}；反應路徑需要逐月單期報酬")
        result = fama_macbeth(panel, factor=factor, target=target,
                              period=period, lags=lags, controls=controls)
        rows.append({
            "horizon_months": int(horizon),
            "periods": result["periods"],
            "mean": result["mean"],
            "se": result["se"],
            "t_stat": result["t_stat"],
            "p_value": result["p_value"],
        })
    return pd.DataFrame(rows)


def cumulative_from_path(panel: pd.DataFrame, *, factor: str,
                         target_prefix: str = "fwd_", horizons=DEFAULT_HORIZONS,
                         period: str = "period", lags: int = 6,
                         controls: list[str] | None = None) -> dict:
    """把反應路徑加總成累積效果，並正確處理各期之間的共變異。

    不能把各期的標準誤直接平方相加——那等於假設各期係數互相獨立。
    正確做法是**先把逐期係數加起來成為一條新序列，再對它做 Newey-West**，
    共變異就自然包含在內了。

    只有各領先期**都**估得出係數的月份才會進入加總，因此回傳的
    ``periods`` 可能小於單期的月數。``periods_dropped`` 把這件事攤開講：
    掉太多就代表累積值算在一個非代表性的子樣本上，不能與單期並排解讀。
    """
    per_horizon = {}
    for horizon in horizons:
        target = f"{target_prefix}{horizon}"
        result = fama_macbeth(panel, factor=factor, target=target,
                              period=period, lags=lags, controls=controls)
        per_horizon[horizon] = result["coefficients"]

    frame = pd.DataFrame(per_horizon)
    available = int(len(frame))
    frame = frame.dropna()
    if frame.empty:
        return {"periods": 0, "periods_dropped": available,
                "cumulative": float("nan"), "se": float("nan"),
                "t_stat": float("nan"), "p_value": float("nan")}
    summed = frame.sum(axis=1)
    mean = float(summed.mean())
    se = newey_west_se(summed, lags=lags)
    t_stat = mean / se if se and np.isfinite(se) else float("nan")
    return {
        "periods": int(summed.size),
        "periods_dropped": available - int(summed.size),
        "horizons": [int(h) for h in horizons],
        "cumulative": mean,
        "se": se,
        "t_stat": float(t_stat),
        "p_value": _two_sided_p(t_stat),
        "newey_west_lags": int(min(lags, summed.size - 1)),
        # 逐期的累積係數。呼叫端必須拿它做 M4 的逐年刪除稽核——
        # 對自己喜歡的結果不做這一步，等於只在不喜歡的結果上套懷疑。
        "series": summed,
    }


def long_only_excess(panel: pd.DataFrame, *, factor: str, target: str,
                     period: str = "period", lags: int = 6) -> dict:
    """只做多的超額報酬：``mean(y | 訊號) − mean(y | 當期全體)``。

    **為什麼一定要另外算這個。** Fama-MacBeth 的係數 ``b`` 是
    「有訊號組 − 無訊號組」的**多空價差**，不是任何人能拿到的報酬。
    真正可部署的是「買進有訊號的那些、相對於買進全體」，其關係為

        long_only = (1 − p) × b        （p ＝ 當期有訊號的比例）

    因此**選擇性越高的訊號，即使多空價差較小，只做多的超額報酬反而可能較大**。
    只看 ``b`` 排序會把這件事完全弄反，而 ``b`` 又是統計上最自然的輸出，
    所以這個陷阱很容易踩。

    這裡逐期直接計算而非套用 ``(1−p)`` 換算，因為逐期的 ``p`` 本來就在變動。
    """
    required = {period, factor, target}
    if not required <= set(panel.columns):
        raise ValueError(f"panel 需含 {sorted(required)}")

    data = panel[[period, factor, target]].dropna()
    per_period, fractions = {}, {}
    for key, group in data.groupby(period, sort=True):
        if len(group) < MIN_CROSS_SECTION:
            continue
        treated = group[group[factor] > 0]
        if treated.empty or len(treated) == len(group):
            continue
        per_period[key] = float(treated[target].mean() - group[target].mean())
        fractions[key] = float(len(treated) / len(group))

    if not per_period:
        return {"periods": 0, "mean": float("nan"), "se": float("nan"),
                "t_stat": float("nan"), "p_value": float("nan"),
                "treated_fraction": float("nan"),
                "coefficients": pd.Series(dtype=float)}

    series = pd.Series(per_period, dtype=float).sort_index()
    mean = float(series.mean())
    se = newey_west_se(series, lags=lags)
    t_stat = mean / se if se and np.isfinite(se) else float("nan")
    return {
        "periods": int(series.size),
        "mean": mean,
        "se": se,
        "t_stat": float(t_stat),
        "p_value": _two_sided_p(t_stat),
        "treated_fraction": float(pd.Series(fractions).mean()),
        "coefficients": series,
    }


def _two_sided_p(t_stat: float) -> float:
    """常態近似的雙尾 p 值。

    月頻樣本約 130 期，t 與常態的差別在小數第三位，
    引入 scipy 只為了這點差異不划算——本專案不依賴 scipy。
    """
    if not np.isfinite(t_stat):
        return float("nan")
    # 標準常態 CDF 以 erf 表示；math.erf 對純量足夠且無額外相依
    from math import erf, sqrt
    return float(2.0 * (1.0 - 0.5 * (1.0 + erf(abs(t_stat) / sqrt(2.0)))))
