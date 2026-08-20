"""H11b：營收訊號的**資訊時點**複製（`research/HYPOTHESES.md` H11b）。

與 H11 的唯一差別
-----------------
H11 把決策日設在「營收公布月的**月末**」。但法定申報期限是次月 10 日前，
因此那個慣例比資訊實際可得晚了約 14 個交易日。診斷
（`scripts/diagnose_revenue_disclosure_window.py`）測得被跳過那段以 H11
自己的估計式為 **+0.842%／月（t=7.56）**，幾乎等於一整個月的效果。

H11b 把整個月頻格點平移：

    H11    ...──月末(m)──────月末(m+1)──────月末(m+2)...
    H11b   ...──E(m)─────────E(m+1)─────────E(m+2)...

    E(R) = 「營收月 R 的法定期限（R+1 月 10 日）結束後，
             第一個交易日的**開盤**」  ← 由 M23 時鐘統一決定

格點間距仍是一個月，因此左邊變數依然是**單期**報酬、依然不重疊（M7）。

**其他一律不動**：營收定義、universe、流動性與規模控制、基準、horizons、
Newey-West lags、交易成本。不加法人、不加動能、不加布林、不調門檻。
本腳本刻意**直接匯入** `run_fama_macbeth_factors` 的函式，
讓「只改了一件事」不是靠人工比對而是靠共用程式碼保證。

軌別
----
**Discovery / development（M22）。不是 OOS 確認。**
我們是在已經看過「早窗很強」之後才設計這個實驗的，因此即使結果漂亮，
它也只能是機制證據。真正的確認仍然只能靠 forward。

事前預期（寫在看到結果之前）
----------------------------
- 現象應成立。若早窗效果顯著小於 +0.842%，**先查實作，不要先解釋結果**。
- 經濟大概率仍不足。粗估淨額約 +0.84%，安全門檻 1.0%。
  **若結果大幅超過門檻，第一反應應該是懷疑實作。**
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.strategy import apply_total_return_adjustment, split_adjust  # noqa: E402
from research.episodes import leave_one_out_audit  # noqa: E402
from research.fama_macbeth import (  # noqa: E402
    DEFAULT_HORIZONS, build_forward_panel, cumulative_from_path, response_path,
)
from research.information_clock import earliest_executable_session  # noqa: E402
import scripts.run_h04_h06_event_study as H04  # noqa: E402
# **刻意共用 H11 的實作**：只改格點，判準與成本計算一字不動。
from scripts.run_fama_macbeth_factors import (  # noqa: E402
    NEWEY_WEST_LAGS, deployable, excess_returns,
)


def execution_grid(sessions: pd.DatetimeIndex,
                   revenue_periods: pd.PeriodIndex) -> pd.DataFrame:
    """每個營收月 R 對應的可執行交易日 E(R)。

    E(R) 由 M23 時鐘給出：嚴格晚於 R+1 月 10 日結束的第一個交易日。
    回傳依 R 排序、且 E(R) 嚴格遞增的格點（同一個交易日不得出現兩次）。
    """
    rows = []
    for period in sorted(set(revenue_periods)):
        session = earliest_executable_session("monthly_revenue", period, sessions)
        if session is not None:
            rows.append({"period": period, "execution": session})
    grid = pd.DataFrame(rows).sort_values("period").reset_index(drop=True)
    if grid["execution"].duplicated().any():
        raise ValueError("同一個交易日對應到多個營收月，格點定義有誤")
    return grid


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "research/results/h11b_revenue_timing.json")
    args = parser.parse_args()

    prices = pd.read_parquet(
        args.snapshot / "prices.parquet",
        columns=["stock_id", "trade_date", "open", "close", "turnover"])
    prices["stock_id"] = prices["stock_id"].astype(str)
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    closes = prices.pivot(index="trade_date", columns="stock_id",
                          values="close").sort_index()
    opens = prices.pivot(index="trade_date", columns="stock_id",
                         values="open").sort_index()
    turnover = prices.pivot(index="trade_date", columns="stock_id",
                            values="turnover").sort_index()

    dividends = pd.read_parquet(args.snapshot / "dividend_events.parquet")
    dividends["stock_id"] = dividends["stock_id"].astype(str)
    dividends["ex_date"] = pd.to_datetime(dividends["ex_date"])
    adjusted = apply_total_return_adjustment(closes.apply(split_adjust), dividends)
    # 還原後的開盤價。同一交易日的 open/close 比值不受除權息與分割影響，
    # 因此可以直接套到還原收盤序列上。M23 的可執行點是**開盤**。
    adjusted_open = adjusted * (opens / closes.where(closes > 0))

    revenue = pd.read_parquet(args.snapshot / "monthly_revenue.parquet")
    revenue["stock_id"] = revenue["stock_id"].astype(str)
    revenue["period"] = pd.PeriodIndex(revenue["year_month"].astype(str), freq="M")

    sessions = pd.DatetimeIndex(closes.index)
    grid = execution_grid(sessions, revenue["period"])
    grid = grid[grid["execution"].isin(sessions)]
    dates = pd.DatetimeIndex(grid["execution"])
    periods = pd.PeriodIndex(grid["period"])

    # ── 格點上的價格、報酬、合格遮罩、規模 ────────────────────────
    grid_price = adjusted_open.reindex(dates)
    # 與 H11 完全相同的慣例：``pct_change`` 給「上一格 → 本格」的**回顧**報酬，
    # 前瞻對齊由 ``build_forward_panel`` 的 ``shift(-horizon)`` 負責，
    # 於是 fwd_1[i] = 從 E(R_i) 開盤到 E(R_{i+1}) 開盤。
    #
    # **不要在這裡再 shift 一次。** 第一版多做了一次 ``shift(-1)``，
    # 等於整條反應路徑往後推一期，M+1 量到的其實是 M+2。
    # 抓到它的是事前登記的預期：「若早窗效果顯著小於 +0.842%，先查實作」——
    # 當時 M+1 只有 +0.680%，比單獨一段早窗還小，在算術上不可能。
    grid_return = grid_price.pct_change(fill_method=None)

    daily_mask = H04.liquid_universe_mask(closes, turnover)
    mask = daily_mask.reindex(dates).reindex(columns=grid_price.columns)
    mask = mask.fillna(False).astype(bool)

    size = np.log1p(turnover.rolling(20).mean()).reindex(dates)
    size = size.reindex(columns=grid_price.columns)

    # 只留當月確實有申報月營收者（排除 ETF／受益憑證），與 H11 的
    # --operating-only 同一個理由：否則「有無營收資料」會被誤讀成
    # 「營收成長與否」。這是 H11 的頭條規格，因此 H11b 沿用。
    has_record = pd.DataFrame(False, index=dates, columns=grid_price.columns)
    for date, period in zip(dates, periods):
        ids = revenue.loc[revenue["period"] == period, "stock_id"]
        present = has_record.columns.intersection(pd.Index(ids.unique()))
        has_record.loc[date, present] = True
    mask = mask & has_record

    benchmark = grid_return.where(mask).mean(axis=1)
    eligible_excess = excess_returns(grid_return, benchmark, mask)
    forward_excess = excess_returns(grid_return, benchmark)   # M13：不套未來合格條件

    signals = {}
    for name, threshold in (("rev_yoy_positive", 0.0), ("rev_yoy_over_20", 20.0)):
        frame = pd.DataFrame(0.0, index=dates, columns=grid_price.columns)
        rows = revenue[revenue["yoy_pct"].notna()]
        for date, period in zip(dates, periods):
            hit = rows.loc[(rows["period"] == period)
                           & (rows["yoy_pct"] > threshold), "stock_id"]
            present = frame.columns.intersection(pd.Index(hit.unique()))
            frame.loc[date, present] = 1.0
        signals[name] = frame

    report = {
        "study": "H11b — revenue signal replicated on the information-availability clock",
        "track": "discovery / development (M22) — NOT an out-of-sample confirmation",
        "only_change_vs_h11": ("decision/execution moved from the month end of the "
                               "disclosure month to the open of the first session "
                               "strictly after the statutory deadline (M23)"),
        "unchanged_vs_h11": ["revenue definition", "universe", "liquidity screen",
                             "size control", "benchmark", "horizons",
                             "newey_west_lags", "transaction cost"],
        "execution_rule": "research.information_clock:earliest_executable_session",
        "entry_price": "adjusted open of the execution session",
        "newey_west_lags": NEWEY_WEST_LAGS,
        "horizons_months": list(DEFAULT_HORIZONS),
        "grid_points": int(len(dates)),
        "grid_first": str(dates[0].date()),
        "grid_last": str(dates[-1].date()),
        "prior_expectation": ("phenomenon expected to hold (~+0.84%/month early "
                              "window); economic significance expected to remain "
                              "BELOW the 1.0% safety threshold. A large overshoot "
                              "should be treated as an implementation bug first."),
        "results": [],
    }

    for signal_name, signal in signals.items():
        panel = build_forward_panel(
            signal=signal, decision_frame=eligible_excess,
            forward_frame=forward_excess, controls={"log_size": size})
        for spec_name, controls in (("raw", []), ("size_controlled", ["log_size"])):
            path = response_path(panel, factor="signal", lags=NEWEY_WEST_LAGS,
                                 controls=controls)
            cumulative = cumulative_from_path(panel, factor="signal",
                                              lags=NEWEY_WEST_LAGS,
                                              controls=controls)
            print(f"{signal_name} / {spec_name}: {len(panel):,} obs, "
                  f"{panel['signal'].sum():,.0f} treated, "
                  f"{panel['period'].nunique()} grid points", flush=True)
            entry = {
                "signal": signal_name,
                "specification": spec_name,
                "controls": controls,
                "observations": int(len(panel)),
                "treated_observations": int(panel["signal"].sum()),
                "grid_points": int(panel["period"].nunique()),
                "path": [
                    {"horizon_months": int(r["horizon_months"]),
                     "periods": int(r["periods"]),
                     "mean_pct": round(r["mean"] * 100, 4),
                     "se_pct": round(r["se"] * 100, 4),
                     "t_stat": round(r["t_stat"], 3),
                     "p_value": round(r["p_value"], 4)}
                    for _, r in path.iterrows()
                ],
                "cumulative_6m": {
                    "periods": cumulative["periods"],
                    "mean_pct": round(cumulative["cumulative"] * 100, 4),
                    "se_pct": round(cumulative["se"] * 100, 4),
                    "t_stat": round(cumulative["t_stat"], 3),
                    "p_value": round(cumulative["p_value"], 4),
                },
            }
            if spec_name == "size_controlled":
                # 只做多、持有 4 個月、扣一次完整換手成本（與 H11 同一個函式）
                entry["deployable"] = deployable(panel, controls)
                audit = leave_one_out_audit(
                    pd.DataFrame({
                        "event_date": pd.DatetimeIndex(panel["period"]),
                        "value": panel["fwd_1"],
                    }), "value", by="year")
                entry["leave_one_year_out"] = {
                    "years": int(audit["groups"]),
                    "survives": bool(audit["conclusion_survives_any_single_removal"]),
                    "flips_sign_groups": int(audit["flips_sign_groups"]),
                    "most_influential_year": audit["most_influential_group"],
                    "mean_without_it_pct": round(audit["mean_without_it"] * 100, 4),
                }
            report["results"].append(entry)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(f"\nwritten: {args.output}")


if __name__ == "__main__":
    main()
