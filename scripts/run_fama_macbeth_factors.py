"""M7：月頻 Fama-MacBeth 與報酬反應路徑，取代長窗事件研究。

規範：`docs/RESEARCH_METHOD_AMENDMENTS.md` M7

**為什麼要換掉事件研究的做法**

`research/HYPOTHESES.md` 曾以「137 個相異事件日、120 日窗重疊 5/6 →
有效獨立觀測僅約 23 個」判定月營收因子「功效不足，無法判定」。
那個推論把問題壓縮成單一時間序列，丟掉了每個月上千檔股票的橫斷面資訊。

這裡改成逐月橫斷面迴歸

    ExcessReturn[i, m+k] = a[m] + b[m] * Signal[i, m] + e

其中 **``m+k`` 是第 k 個月的單月報酬，不是 k 個月的累積報酬**。
於是 ``b[m]`` 與 ``b[m+1]`` 在左邊變數上完全不重疊，重疊問題在設計上消失，
Newey-West 只需處理殘存的市況自相關。

反應路徑（第 1~6 個月各自的係數）是比累積報酬更有用的診斷：
資訊擴散應該呈現逐月遞減的正效果；若前五個月都是零、第六個月突然跳出來，
那幾乎可以確定不是資訊擴散。

試驗次數：0。事件定義與既有研究完全相同，改的只有估計式。
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
    DEFAULT_HORIZONS, build_forward_panel, cumulative_from_path,
    long_only_excess, response_path,
)
from research.inference import MIN_TRADEABLE_EDGE, ROUND_TRIP_COST  # noqa: E402

# 效果集中在前 4 個月（見反應路徑），因此以 4 個月為部署持有期估算成本
DEPLOYABLE_HORIZONS = (1, 2, 3, 4)
import scripts.run_h04_h06_event_study as H04  # noqa: E402

NEWEY_WEST_LAGS = 6      # 事前固定＝最長領先期，不得為了 p 值調整


def monthly_frames(closes: pd.DataFrame, turnover: pd.DataFrame,
                   adjusted: pd.DataFrame):
    """把日頻面板壓成月頻：月末還原價、月報酬、月末合格遮罩。"""
    month_end = adjusted.resample("ME").last()
    monthly_return = month_end.pct_change(fill_method=None)
    mask = H04.liquid_universe_mask(closes, turnover).resample("ME").last()
    mask = mask.reindex(columns=month_end.columns).fillna(False).astype(bool)
    return month_end, monthly_return, mask


def excess_returns(monthly_return: pd.DataFrame, benchmark: pd.Series,
                   mask: pd.DataFrame | None = None) -> pd.DataFrame:
    """個股月報酬減去同月基準報酬；``mask`` 給定時只保留該月合格者。

    **前瞻報酬不套用遮罩。** 決策發生在 m 月月末，此時無從得知該股在 m+3 月
    是否仍然合格；若把前瞻報酬也用未來的合格條件過濾，等於只保留「後來還活著
    且仍夠流動」的股票，那是存活者偏差，方向偏正。

    因此遮罩只用在**決策時點**（決定誰進得了當期橫斷面），
    前瞻報酬一律取未遮罩版本。兩種算法的差距由 `--survivorship-check` 量出來。
    """
    aligned = benchmark.reindex(monthly_return.index)
    excess = monthly_return.sub(aligned, axis=0)
    return excess.where(mask) if mask is not None else excess


def has_revenue_record(snapshot: Path, index: pd.DatetimeIndex,
                       columns: pd.Index) -> pd.DataFrame:
    """該股在該決策月是否有月營收申報（＝是否為營運中的上市櫃公司）。

    ETF、受益憑證、TDR 沒有月營收，永遠會被歸進「未成長」的對照組，
    使「有沒有這筆資料」混進「營收有沒有成長」的估計。
    """
    revenue = pd.read_parquet(snapshot / "monthly_revenue.parquet",
                              columns=["stock_id", "year_month", "revenue"])
    revenue["stock_id"] = revenue["stock_id"].astype(str)
    period = pd.PeriodIndex(revenue["year_month"].astype(str), freq="M")
    present = revenue.assign(decision=(period + 1).to_timestamp(how="start"),
                             flag=1.0)
    wide = present.pivot_table(index="decision", columns="stock_id",
                               values="flag", aggfunc="max")
    return _align_month_end(wide, index, columns).astype(bool)


def revenue_signal(snapshot: Path, index: pd.DatetimeIndex,
                   columns: pd.Index, minimum_yoy: float) -> pd.DataFrame:
    """月末已知的月營收訊號。

    ``year_month`` = M 的營收於 M+1 月 10 日前公布，因此在 **M+1 月月末**
    已經確定可知。決策月 m 用的是 ``year_month = m - 1`` 的營收，
    而報酬從 m+1 月起算——完全不觸及未公布資訊。
    """
    revenue = pd.read_parquet(snapshot / "monthly_revenue.parquet")
    revenue["stock_id"] = revenue["stock_id"].astype(str)
    period = pd.PeriodIndex(revenue["year_month"].astype(str), freq="M")
    # 公布月＝資料月 + 1；月末已必然公布，故以該月為決策月
    revenue = revenue.assign(decision=(period + 1).to_timestamp(how="start"))
    revenue = revenue[revenue["yoy_pct"].notna()]
    flag = revenue.assign(signal=(revenue["yoy_pct"] > minimum_yoy).astype(float))
    wide = flag.pivot_table(index="decision", columns="stock_id",
                            values="signal", aggfunc="max")
    return _align_month_end(wide, index, columns)


def streak_signal(snapshot: Path, daily_mask: pd.DataFrame,
                  index: pd.DatetimeIndex, columns: pd.Index) -> pd.DataFrame:
    """投信連買達標（累計 100 張）於該月內曾發生 → 該月月末訊號為 1。

    定義沿用 `run_h04_h06_event_study.institutional_events(kind="streak")`，
    一字不改；只是把日頻觸發聚合到月末的決策時點。
    """
    events = H04.institutional_events(snapshot, daily_mask, "streak")
    if events.empty:
        return pd.DataFrame(0.0, index=index, columns=columns)
    wide = events.assign(signal=1.0).pivot_table(
        index="event_date", columns="stock_id", values="signal", aggfunc="max")
    return _align_month_end(wide, index, columns)


def _align_month_end(wide: pd.DataFrame, index: pd.DatetimeIndex,
                     columns: pd.Index) -> pd.DataFrame:
    """把任意日期的訊號聚合到月頻索引的月末，缺值即「未觸發」＝0。

    **必須先轉成月份再對齊**，不能直接 ``reindex(index)``：``index`` 來自
    ``resample("ME")``，是**日曆**月末（如 2015-01-31），而訊號落在交易日上，
    兩者只有約六成的月份會相等。先前的版本就是這樣把四成的月份洗成全 0，
    使那些月的橫斷面係數變成無定義而被丟掉。
    """
    aligned = wide.reindex(columns=columns)
    aligned = aligned.groupby(
        pd.DatetimeIndex(aligned.index).to_period("M")).max()
    target = pd.PeriodIndex(index, freq="M")
    aligned = aligned.reindex(target).fillna(0.0)
    aligned.index = index
    return aligned


def deployable(panel: pd.DataFrame, controls: list[str]) -> dict:
    """只做多、持有 4 個月、扣掉一次完整換手成本後還剩多少（M5）。

    多空價差不是任何人拿得到的報酬；能部署的是「買進有訊號者、相對於買進全體」。
    這裡把前 4 個月的只做多超額報酬加總，再扣一次 1.185% 的完整換手成本。

    ``controls`` 目前不參與：只做多的超額報酬是實際持有的結果，
    無法「控制掉」規模——真的下單時規模傾斜會原封不動地跟著進來。
    這一點正是它與迴歸係數的差別，也是它才是部署判準的理由。
    """
    per_horizon, total = [], 0.0
    for horizon in DEPLOYABLE_HORIZONS:
        result = long_only_excess(panel, factor="signal",
                                  target=f"fwd_{horizon}", lags=NEWEY_WEST_LAGS)
        total += result["mean"]
        per_horizon.append({
            "horizon_months": int(horizon),
            "mean_pct": round(result["mean"] * 100, 4),
            "t_stat": round(result["t_stat"], 3),
            "treated_fraction": round(result["treated_fraction"], 4),
        })
    net = total - ROUND_TRIP_COST
    return {
        "horizons": list(DEPLOYABLE_HORIZONS),
        "path": per_horizon,
        "gross_pct": round(total * 100, 4),
        "round_trip_cost_pct": round(ROUND_TRIP_COST * 100, 4),
        "net_pct": round(net * 100, 4),
        "economic_threshold_pct": round(MIN_TRADEABLE_EDGE * 100, 4),
        "clears_threshold": bool(total > MIN_TRADEABLE_EDGE),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "research/results/fama_macbeth_factors.json")
    parser.add_argument("--survivorship-check", action="store_true",
                        help="對照用：前瞻報酬也套用未來的合格條件（含存活者偏差）")
    parser.add_argument("--operating-only", action="store_true",
                        help="橫斷面只留當月有月營收申報者，排除 ETF／受益憑證等")
    args = parser.parse_args()

    prices = pd.read_parquet(
        args.snapshot / "prices.parquet",
        columns=["stock_id", "trade_date", "close", "turnover"])
    prices["stock_id"] = prices["stock_id"].astype(str)
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    closes = prices.pivot(index="trade_date", columns="stock_id",
                          values="close").sort_index()
    turnover = prices.pivot(index="trade_date", columns="stock_id",
                            values="turnover").sort_index()

    dividends = pd.read_parquet(args.snapshot / "dividend_events.parquet")
    dividends["stock_id"] = dividends["stock_id"].astype(str)
    dividends["ex_date"] = pd.to_datetime(dividends["ex_date"])
    adjusted = apply_total_return_adjustment(closes.apply(split_adjust), dividends)

    daily_mask = H04.liquid_universe_mask(closes, turnover)
    month_end, monthly_return, mask = monthly_frames(closes, turnover, adjusted)

    # 規模／流動性代理：月末的近 20 日均成交金額（取對數）。
    # 台股月營收高成長股天生偏中小型，不控制就分不出「營收資訊」與「小型股溢酬」。
    size = np.log1p(turnover.rolling(20).mean()).resample("ME").last()
    size = size.reindex(index=month_end.index, columns=month_end.columns)

    # 基準只需一個。逐期迴歸的截距已吸收全體共有的報酬，因此把左邊換成
    # 「減 0050」或「減等權 universe」得到的因子係數**完全相同**（實測驗證）。
    # 基準選擇只影響組合層級的績效宣稱，那部分在 run_method_audit.py 兩個都報。
    if args.operating_only:
        # 對照組若混入 ETF／受益憑證，它們永遠沒有月營收＝永遠 signal=0，
        # 於是「有無營收資料」會被誤讀成「營收成長與否」。
        # 只留當月確實有申報月營收者，三個因子的橫斷面才是同一個母體。
        mask = mask & has_revenue_record(
            args.snapshot, month_end.index, month_end.columns)

    benchmark = monthly_return.where(mask).mean(axis=1)
    eligible_excess = excess_returns(monthly_return, benchmark, mask)
    forward_excess = excess_returns(monthly_return, benchmark)
    if args.survivorship_check:
        # 對照組：前瞻報酬也套用未來的合格條件（＝先前的寫法）。
        # 兩者的差就是存活者偏差的量級。
        forward_excess = eligible_excess

    signals = {
        "rev_yoy_over_20": revenue_signal(args.snapshot, month_end.index,
                                          month_end.columns, 20.0),
        "rev_yoy_positive": revenue_signal(args.snapshot, month_end.index,
                                           month_end.columns, 0.0),
        "invest_streak": streak_signal(args.snapshot, daily_mask,
                                       month_end.index, month_end.columns),
    }
    # M9 在橫斷面設定下的正確形式：控制規模，而不是換基準。
    specifications = {"raw": [], "size_controlled": ["log_size"]}

    report = {
        "study": "monthly Fama-MacBeth response path (M7)",
        "amendments": "docs/RESEARCH_METHOD_AMENDMENTS.md",
        "design": ("cross-sectional regression each month on a single-month "
                   "forward excess return; horizons are non-overlapping by "
                   "construction, so the 120-day overlap problem does not arise"),
        "benchmark_note": ("the per-period intercept absorbs any return component "
                           "common to all stocks, so the factor coefficient is "
                           "identical under a 0050 or an equal-weight benchmark; "
                           "benchmark choice matters for portfolio-level claims "
                           "only, which run_method_audit.py reports under both"),
        "newey_west_lags": NEWEY_WEST_LAGS,
        "horizons_months": list(DEFAULT_HORIZONS),
        "new_trials": 0,
        "results": [],
    }

    report["forward_returns_masked_by_future_eligibility"] = bool(
        args.survivorship_check)

    for signal_name, signal in signals.items():
        panel = build_forward_panel(
            signal=signal, decision_frame=eligible_excess,
            forward_frame=forward_excess, controls={"log_size": size})
        for spec_name, controls in specifications.items():
            path = response_path(panel, factor="signal", lags=NEWEY_WEST_LAGS,
                                 controls=controls)
            cumulative = cumulative_from_path(panel, factor="signal",
                                              lags=NEWEY_WEST_LAGS,
                                              controls=controls)
            treated = float(panel["signal"].sum())
            print(f"{signal_name} / {spec_name}: {len(panel):,} obs, "
                  f"{treated:,.0f} treated, {panel['period'].nunique()} months",
                  flush=True)
            report["results"].append({
                "signal": signal_name,
                "specification": spec_name,
                "controls": controls,
                "observations": int(len(panel)),
                "treated_observations": int(treated),
                "months": int(panel["period"].nunique()),
                "path": [
                    {"horizon_months": int(r["horizon_months"]),
                     "months": int(r["periods"]),
                     "mean_pct": round(r["mean"] * 100, 4),
                     "se_pct": round(r["se"] * 100, 4),
                     "t_stat": round(r["t_stat"], 3),
                     "p_value": round(r["p_value"], 4)}
                    for _, r in path.iterrows()
                ],
                "cumulative_6m": {
                    "months": cumulative["periods"],
                    "months_dropped": cumulative["periods_dropped"],
                    "mean_pct": round(cumulative["cumulative"] * 100, 4),
                    "se_pct": round(cumulative["se"] * 100, 4),
                    "t_stat": round(cumulative["t_stat"], 3),
                    "p_value": round(cumulative["p_value"], 4),
                },
                # M5：真正能決定「值不值得做」的是只做多、扣成本後的數字
                "deployable_long_only_4m": deployable(panel, controls),
                # M4 套用在 M7 上：好結果也要過逐年刪除，否則就是選擇性懷疑
                "leave_one_year_out": leave_one_out_audit(
                    pd.DataFrame({
                        "event_date": pd.DatetimeIndex(cumulative["series"].index),
                        "value": cumulative["series"].to_numpy(),
                    }), "value", by="year"),
            })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                      default=str) + "\n", encoding="utf-8")
    print(f"\nwritten: {args.output}")


if __name__ == "__main__":
    main()
