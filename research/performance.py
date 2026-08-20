"""績效指標與 F1 驗收判準（SPEC §8.2、§8.3、§9.2）。

刻意與 `agent/backtest.py::perf_metrics` 分開：那一套服務線上 app 的波段策略，
而這裡要對應的是**已凍結的 MOM-1 驗收條文**。共用會讓其中一邊的修改
無聲地改變另一邊的判決。

§8.3 要求組合績效用月報酬 bootstrap、block 長度**事前固定為 6 個月**，
並報 95% 信賴區間而不是只報一個 Sharpe。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

SESSIONS_PER_YEAR = 252
MONTHS_PER_BLOCK = 6          # §8.3 事前固定
BOOTSTRAP_DRAWS = 1000


def _returns(nav: pd.Series) -> pd.Series:
    return nav.astype(float).pct_change().dropna()


def monthly_returns(nav: pd.Series) -> pd.Series:
    """月報酬。用月末淨值相除，不是日報酬複合，避免累積浮點誤差。"""
    month_end = nav.astype(float).resample("ME").last().dropna()
    return month_end.pct_change().dropna()


def max_drawdown(nav: pd.Series) -> float:
    series = nav.astype(float)
    peak = series.cummax()
    return float((series / peak - 1.0).min())


def drawdown_recovery(nav: pd.Series) -> dict:
    """最長回撤與恢復時間（§8.2）。單位是交易日。"""
    series = nav.astype(float)
    peak = series.cummax()
    underwater = series < peak
    longest, current = 0, 0
    for flag in underwater:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return {"longest_underwater_sessions": int(longest),
            "currently_underwater": bool(underwater.iloc[-1])}


def core_metrics(nav: pd.Series) -> dict:
    """§8.2 的第一組：總報酬、年化、波動、Sharpe、Sortino、MDD、Calmar。"""
    series = nav.astype(float)
    daily = _returns(series)
    years = len(series) / SESSIONS_PER_YEAR
    total = float(series.iloc[-1] / series.iloc[0] - 1.0)
    cagr = float((series.iloc[-1] / series.iloc[0]) ** (1 / years) - 1.0) if years > 0 else float("nan")
    volatility = float(daily.std(ddof=1) * np.sqrt(SESSIONS_PER_YEAR)) if len(daily) > 1 else float("nan")
    downside = daily[daily < 0]
    downside_vol = (float(downside.std(ddof=1) * np.sqrt(SESSIONS_PER_YEAR))
                    if len(downside) > 1 else float("nan"))
    drawdown = max_drawdown(series)
    return {
        "total_return": total,
        "cagr": cagr,
        "annualised_volatility": volatility,
        # 無風險利率取 0：2008~2014 台灣定存利率極低，且基準與策略同樣處理，
        # 兩邊相減時它會抵銷。
        "sharpe": float(cagr / volatility) if volatility else float("nan"),
        "sortino": float(cagr / downside_vol) if downside_vol else float("nan"),
        "max_drawdown": drawdown,
        "calmar": float(cagr / abs(drawdown)) if drawdown else float("nan"),
        **drawdown_recovery(series),
    }


def relative_metrics(nav: pd.Series, benchmark: pd.Series) -> dict:
    """§8.2 的第二組：相對基準的年化超額、tracking error、IR、beta。"""
    aligned = pd.concat([nav.astype(float), benchmark.astype(float)],
                        axis=1, keys=["strategy", "benchmark"]).dropna()
    strategy_daily = aligned["strategy"].pct_change().dropna()
    benchmark_daily = aligned["benchmark"].pct_change().dropna()
    difference = (strategy_daily - benchmark_daily).dropna()
    years = len(aligned) / SESSIONS_PER_YEAR
    strategy_cagr = float((aligned["strategy"].iloc[-1] / aligned["strategy"].iloc[0])
                          ** (1 / years) - 1.0)
    benchmark_cagr = float((aligned["benchmark"].iloc[-1] / aligned["benchmark"].iloc[0])
                           ** (1 / years) - 1.0)
    tracking_error = float(difference.std(ddof=1) * np.sqrt(SESSIONS_PER_YEAR))
    variance = float(benchmark_daily.var(ddof=1))
    beta = (float(strategy_daily.cov(benchmark_daily) / variance)
            if variance else float("nan"))
    return {
        "annualised_excess": strategy_cagr - benchmark_cagr,
        "tracking_error": tracking_error,
        "information_ratio": (float((strategy_cagr - benchmark_cagr) / tracking_error)
                              if tracking_error else float("nan")),
        "beta": beta,
        "benchmark_cagr": benchmark_cagr,
    }


def monthly_profile(nav: pd.Series) -> dict:
    monthly = monthly_returns(nav)
    annual = nav.astype(float).resample("YE").last().pct_change().dropna()
    first_year_nav = nav.astype(float).resample("YE").last()
    if len(first_year_nav):
        first = float(first_year_nav.iloc[0] / nav.astype(float).iloc[0] - 1.0)
        annual = pd.concat([pd.Series({first_year_nav.index[0]: first}), annual])
        annual = annual[~annual.index.duplicated(keep="first")].sort_index()
    return {
        "months": int(len(monthly)),
        "monthly_win_rate": float((monthly > 0).mean()) if len(monthly) else float("nan"),
        "annual_returns": {str(index.year): float(value) for index, value in annual.items()},
    }


def trade_concentration(trades: pd.DataFrame, *, column: str = "net_pnl") -> dict:
    """§8.2／§9.2：前 N 筆佔比、移除最佳 5 筆後的淨損益、單一年份佔毛利比。

    形狀比摘要統計誠實——舊策略 475 筆交易裡 10 筆決定了 96% 的結果，
    而 Sharpe 0.919 完全沒有透露這件事。
    """
    if trades.empty or column not in trades:
        return {"trades": 0}
    values = trades[column].astype(float).sort_values(ascending=False)
    total = float(values.sum())
    positives = values[values > 0]
    gross_profit = float(positives.sum())

    by_year = {}
    if "exit_date" in trades:
        years = pd.DatetimeIndex(trades["exit_date"]).year
        profit = trades[column].astype(float).where(lambda s: s > 0, 0.0)
        by_year = profit.groupby(years).sum().to_dict()
    largest_year_share = (max(by_year.values()) / gross_profit
                          if by_year and gross_profit > 0 else float("nan"))

    return {
        "trades": int(len(values)),
        "win_rate": float((values > 0).mean()),
        "net_pnl": total,
        "gross_profit": gross_profit,
        "top1_share_of_gross_profit": (float(values.iloc[0] / gross_profit)
                                       if gross_profit > 0 else float("nan")),
        "top5_share_of_gross_profit": (float(values.head(5).sum() / gross_profit)
                                       if gross_profit > 0 else float("nan")),
        "top10_share_of_gross_profit": (float(values.head(10).sum() / gross_profit)
                                        if gross_profit > 0 else float("nan")),
        "net_pnl_excluding_best_5": float(values.iloc[5:].sum()) if len(values) > 5 else 0.0,
        "largest_year_share_of_gross_profit": float(largest_year_share),
        "gross_profit_by_year": {str(k): float(v) for k, v in by_year.items()},
    }


def block_bootstrap_monthly(nav: pd.Series, *, draws: int = BOOTSTRAP_DRAWS,
                            block_months: int = MONTHS_PER_BLOCK,
                            seed: int = 20260814) -> dict:
    """月報酬的 block bootstrap，回傳年化報酬與 Sharpe 的 95% CI（§8.3）。

    block 長度**事前固定為 6 個月**，不掃描——那正是 M1 要求敏感度分析的反面：
    這裡不是在挑一個好看的 block，而是在遵守一個事前寫死的值。
    """
    monthly = monthly_returns(nav)
    if len(monthly) < block_months * 2:
        return {"draws": 0, "note": "月數不足以做 6 個月 block 重抽"}
    values = monthly.to_numpy(dtype=float)
    blocks = [values[i:i + block_months]
              for i in range(0, len(values) - block_months + 1)]
    rng = np.random.default_rng(seed)
    needed = int(np.ceil(len(values) / block_months))
    annualised, sharpes = np.empty(draws), np.empty(draws)
    for i in range(draws):
        picked = rng.integers(0, len(blocks), size=needed)
        sample = np.concatenate([blocks[j] for j in picked])[:len(values)]
        annualised[i] = (1.0 + sample.mean()) ** 12 - 1.0
        deviation = sample.std(ddof=1)
        sharpes[i] = (sample.mean() * 12) / (deviation * np.sqrt(12)) if deviation else np.nan
    finite = sharpes[np.isfinite(sharpes)]
    return {
        "draws": draws,
        "block_months": block_months,
        "annualised_return_ci95": [float(np.percentile(annualised, 2.5)),
                                   float(np.percentile(annualised, 97.5))],
        "sharpe_ci95": ([float(np.percentile(finite, 2.5)),
                         float(np.percentile(finite, 97.5))]
                        if finite.size else [float("nan")] * 2),
    }


def f1_criteria(*, nav: pd.Series, benchmark: pd.Series,
                trades: pd.DataFrame) -> dict:
    """SPEC §9.2 的六項門檻。**全部通過才算未被否證。**

    門檻一個都沒有放寬——F1 改為否證測試，變的是通過之後可以宣稱什麼，
    不是要多少才算通過。
    """
    strategy = core_metrics(nav)
    reference = core_metrics(benchmark)
    relative = relative_metrics(nav, benchmark)
    concentration = trade_concentration(trades)

    checks = {
        "net_profit_positive": bool(nav.iloc[-1] > nav.iloc[0]),
        "annualised_excess_positive": bool(relative["annualised_excess"] > 0),
        "sharpe_at_least_benchmark": bool(strategy["sharpe"] >= reference["sharpe"]),
        "drawdown_within_5pp_of_benchmark": bool(
            strategy["max_drawdown"] >= reference["max_drawdown"] - 0.05),
        "positive_excluding_best_5_trades": bool(
            concentration.get("net_pnl_excluding_best_5", 0.0) > 0),
        "no_year_over_40pct_of_gross_profit": bool(
            np.isfinite(concentration.get("largest_year_share_of_gross_profit", np.nan))
            and concentration["largest_year_share_of_gross_profit"] <= 0.40),
    }
    return {"checks": checks, "passed": all(checks.values())}
