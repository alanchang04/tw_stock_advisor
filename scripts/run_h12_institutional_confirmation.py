"""H12：法人確認的條件價值（登記見 `research/HYPOTHESES.md` H12）。

**問題**：在同樣有營收改善的公司裡，之後 20 個交易日出現投信確認買超者，
是否比沒有出現的有更高的未來超額報酬？

    Δ_signal = E[R | Rev+, Confirm+] − E[R | Rev+, Confirm−]      ← 主要判準
    Δ_portfolio = E[R | Rev+, Confirm+] − E[R | Rev+]             ← 只報告

## 三個會讓結果失真的地方，都用結構擋住而不是靠自律

**M15 延遲資訊（本假說最危險的一條）。**「營收公布後 20 日內投信累計買超
100 張」在營收公布當日**並不知道**。若一邊用那 20 天決定 Confirm、一邊把那
20 天的報酬算進績效，就是前視——與先前把布林效果放大 8 倍的
「只取後來有 B2 的 B1」是同一類錯誤。因此採 landmark design：
決策時點推到確認窗結束（L = d + 20 個交易日），前瞻報酬自 **L+1** 起算，
並以 `assert_information_precedes_decision` 在 `forward_start <= information_end`
時直接拋錯。

**M19 覆蓋率斷點。** 法人資料 TWSE 自 2015、**TPEX 自 2018**（實測確認）。
把 TPEX 2015~2017 的缺值讀成 `Confirm = 0`，會讓時間、市場別與訊號糾纏。
本輪取 common-coverage：**2018 起、全市場**。
選這個而不是「全期只用 TWSE」，是因為後者需要用**當前**的市場別分類回頭套，
而已下市公司在當前快照裡沒有列，會引入存活者偏差。

**H11 基準必須同期重算。** H11 發表的數字跑在 2015~2026；H12 在 2018 起。
若拿 H12 的結果去比 H11 的published 數字，會把期間差異混進「增量」。
因此本腳本**在同一個 2018+ 樣本內**重算 Revenue+ 基準。

試驗次數：4（條件迴歸 × {未控制, 控制規模} × {多空, 只做多}），事前登記。
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
from research.fama_macbeth import newey_west_se  # noqa: E402
from research.policy import (  # noqa: E402
    assert_information_precedes_decision, landmark_confirmation,
)
import scripts.run_h04_h06_event_study as H04  # noqa: E402

# ── 事前登記、不得更動的常數 ──────────────────────────────────────────
CONFIRMATION_WINDOW = 20          # 交易日；沿用登記，不掃描 10/40
CONFIRMATION_LOTS = 100           # 張；沿用 agent/strategy.py::MIN_INVEST_STREAK_LOTS
SHARES_PER_LOT = 1_000
PRIMARY_HORIZON = 60              # 交易日；依 M12 事前指定
REVENUE_MIN_YOY = 0.0             # 主要規格；>20% 列 robustness
COMMON_COVERAGE_START = "2018-01-01"   # M19：TPEX 法人自 2018 起
NEWEY_WEST_LAGS = 6


def monthly_decision_dates(sessions: pd.DatetimeIndex, start, end) -> list:
    """每月最後一個交易日。營收於次月 10 日前公布，月末必然已知。"""
    frame = pd.Series(sessions, index=sessions)
    month_end = frame.resample("ME").last().dropna()
    return [d for d in month_end
            if pd.Timestamp(start) <= d <= pd.Timestamp(end)]


def revenue_positive(snapshot: Path, decision: pd.Timestamp,
                     minimum_yoy: float) -> set:
    """決策月月末已知的營收改善名單（資料月 = 決策月 − 1）。"""
    revenue = revenue_positive.cache
    period = (decision.to_period("M") - 1)
    rows = revenue[(revenue["period"] == period)
                   & revenue["yoy_pct"].notna()
                   & (revenue["yoy_pct"] > minimum_yoy)]
    return set(rows["stock_id"])


def load_revenue(snapshot: Path) -> pd.DataFrame:
    revenue = pd.read_parquet(snapshot / "monthly_revenue.parquet")
    revenue["stock_id"] = revenue["stock_id"].astype(str)
    revenue["period"] = pd.PeriodIndex(revenue["year_month"].astype(str), freq="M")
    return revenue


def build_panel(snapshot: Path, *, minimum_yoy: float, horizon: int) -> pd.DataFrame:
    prices = pd.read_parquet(snapshot / "prices.parquet",
                             columns=["stock_id", "trade_date", "close", "turnover"])
    prices["stock_id"] = prices["stock_id"].astype(str)
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    closes = prices.pivot(index="trade_date", columns="stock_id", values="close").sort_index()
    turnover = prices.pivot(index="trade_date", columns="stock_id", values="turnover").sort_index()

    dividends = pd.read_parquet(snapshot / "dividend_events.parquet")
    dividends["stock_id"] = dividends["stock_id"].astype(str)
    dividends["ex_date"] = pd.to_datetime(dividends["ex_date"])
    adjusted = apply_total_return_adjustment(closes.apply(split_adjust), dividends)

    inst = pd.read_parquet(snapshot / "institutional.parquet",
                           columns=["stock_id", "trade_date", "invest_net"])
    inst["stock_id"] = inst["stock_id"].astype(str)
    inst["trade_date"] = pd.to_datetime(inst["trade_date"])
    invest = inst.pivot(index="trade_date", columns="stock_id",
                        values="invest_net").reindex_like(closes)

    mask = H04.liquid_universe_mask(closes, turnover)
    sessions = pd.DatetimeIndex(closes.index)
    size = np.log1p(turnover.rolling(20).mean())

    last_usable = sessions[-(horizon + CONFIRMATION_WINDOW + 2)]
    decisions = monthly_decision_dates(sessions, COMMON_COVERAGE_START, last_usable)
    revenue_positive.cache = load_revenue(snapshot)

    rows = []
    for decision in decisions:
        eligible = [s for s in closes.columns if bool(mask.at[decision, s])]
        winners = revenue_positive(snapshot, decision, minimum_yoy) & set(eligible)
        if len(winners) < 30:
            continue
        events = pd.DataFrame({"stock_id": sorted(winners),
                               "event_date": decision})
        landmark = landmark_confirmation(
            events, invest, sessions=sessions,
            window_sessions=CONFIRMATION_WINDOW,
            threshold=CONFIRMATION_LOTS * SHARES_PER_LOT)
        landmark = landmark[landmark["decision_date"].notna()]
        if landmark.empty:
            continue

        entry_position = sessions.get_loc(landmark["decision_date"].iloc[0]) + 1
        exit_position = entry_position + horizon
        if exit_position >= len(sessions):
            continue
        entry, exit_ = sessions[entry_position], sessions[exit_position]
        landmark["forward_start"] = entry
        # M15 守門：前瞻報酬起算日必須嚴格晚於確認資訊結束日
        assert_information_precedes_decision(landmark)

        ids = [s for s in landmark["stock_id"] if s in adjusted.columns]
        forward = (adjusted.loc[exit_, ids] / adjusted.loc[entry, ids] - 1.0)
        # 基準：同期所有合格股票的等權報酬（把市場成分拿掉，方便解讀 2x2）
        universe = [s for s in eligible if s in adjusted.columns]
        reference = float((adjusted.loc[exit_, universe]
                           / adjusted.loc[entry, universe] - 1.0).mean(skipna=True))
        frame = landmark.set_index("stock_id").loc[ids]
        rows.append(pd.DataFrame({
            "decision": decision,
            "stock_id": ids,
            "confirm": frame["confirmed"].astype(float).to_numpy(),
            "excess": (forward - reference).to_numpy(),
            # **規模必須量在決策日 d，不能量在進場日 L+1。**
            # 投信買超本身會推高成交金額，L+1 的量已經含有 20 天的確認買盤——
            # 那是 post-treatment 變數，控制它等於把一部分處理效果也控制掉
            # （collider / mediator）。d 在確認窗之前，是乾淨的 pre-treatment 控制。
            "log_size": size.loc[decision, ids].to_numpy(),
        }))

    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def conditional_regression(panel: pd.DataFrame, *, controls: bool) -> dict:
    """逐月在 Revenue+ 樣本內迴歸 excess 對 confirm（可加 log 規模），再 NW。"""
    coefficients, widths, treated_share = [], [], []
    for decision, group in panel.groupby("decision"):
        data = group.dropna(subset=["excess", "confirm"]
                            + (["log_size"] if controls else []))
        if len(data) < 30 or data["confirm"].nunique() < 2:
            continue
        blocks = [np.ones(len(data)), data["confirm"].to_numpy()]
        if controls:
            blocks.append(data["log_size"].to_numpy())
        design = np.column_stack(blocks)
        if np.linalg.matrix_rank(design) < design.shape[1]:
            continue
        solution, *_ = np.linalg.lstsq(design, data["excess"].to_numpy(), rcond=None)
        coefficients.append(float(solution[1]))
        widths.append(len(data))
        treated_share.append(float(data["confirm"].mean()))

    series = pd.Series(coefficients, dtype=float).dropna()
    if series.empty:
        return {"months": 0}
    se = newey_west_se(series, lags=NEWEY_WEST_LAGS)
    return {
        "months": int(series.size),
        "median_cross_section": int(np.median(widths)),
        "mean_confirmed_share": round(float(np.mean(treated_share)), 4),
        "delta_signal_pct": round(float(series.mean()) * 100, 4),
        "se_pct": round(float(se) * 100, 4) if se and np.isfinite(se) else None,
        "t_stat": round(float(series.mean() / se), 3)
        if se and np.isfinite(se) else None,
    }


def two_by_two(panel: pd.DataFrame) -> dict:
    """逐月算各組平均再對月取平均（等權月份，避免大月支配）。"""
    confirmed, unconfirmed, everyone = [], [], []
    for _, group in panel.groupby("decision"):
        data = group.dropna(subset=["excess"])
        if len(data) < 30 or data["confirm"].nunique() < 2:
            continue
        confirmed.append(float(data.loc[data["confirm"] > 0, "excess"].mean()))
        unconfirmed.append(float(data.loc[data["confirm"] == 0, "excess"].mean()))
        everyone.append(float(data["excess"].mean()))

    def stat(values):
        series = pd.Series(values, dtype=float).dropna()
        se = newey_west_se(series, lags=NEWEY_WEST_LAGS) if len(series) > 1 else np.nan
        return {"mean_pct": round(float(series.mean()) * 100, 4),
                "t_stat": round(float(series.mean() / se), 3)
                if se and np.isfinite(se) else None}

    portfolio = pd.Series(confirmed, dtype=float) - pd.Series(everyone, dtype=float)
    se = newey_west_se(portfolio, lags=NEWEY_WEST_LAGS)
    return {
        "revenue_plus_confirm_plus": stat(confirmed),
        "revenue_plus_confirm_minus": stat(unconfirmed),
        "revenue_plus_all": stat(everyone),
        "delta_portfolio_pct": round(float(portfolio.mean()) * 100, 4),
        "delta_portfolio_t": round(float(portfolio.mean() / se), 3)
        if se and np.isfinite(se) else None,
    }


def breadth(panel: pd.DataFrame) -> dict:
    per_month = panel.groupby("decision").agg(
        revenue_plus=("stock_id", "size"),
        confirmed=("confirm", "sum"))
    return {
        "months": int(len(per_month)),
        "median_revenue_plus_per_month": int(per_month["revenue_plus"].median()),
        "median_confirmed_per_month": int(per_month["confirmed"].median()),
        "min_confirmed_per_month": int(per_month["confirmed"].min()),
        "distinct_stocks": int(panel["stock_id"].nunique()),
        "overall_confirmed_share": round(float(panel["confirm"].mean()), 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "research/results/h12_institutional_confirmation.json")
    args = parser.parse_args()

    report = {
        "study": "H12 institutional confirmation conditional on revenue improvement",
        "registration": "research/HYPOTHESES.md H12",
        "primary_estimand": "E[R | Rev+, Confirm+] - E[R | Rev+, Confirm-]",
        "primary_horizon_sessions": PRIMARY_HORIZON,
        "confirmation": (f"cumulative trust-fund net buy >= {CONFIRMATION_LOTS} lots "
                         f"within {CONFIRMATION_WINDOW} sessions AFTER the decision date; "
                         "forward returns start strictly after that window (M15 landmark)"),
        "coverage": (f"common-coverage universe from {COMMON_COVERAGE_START}: "
                     "TPEx institutional data starts 2018 (verified), so earlier months "
                     "would read missing as Confirm=0 (M19)"),
        "interpretation": ("2015-2026 is contaminated for this hypothesis (H04 origin); "
                           "a surviving effect may only be reported as 'not falsified'"),
        "trials_registered": 4,
        "specifications": {},
    }

    for label, minimum_yoy in (("primary_rev_yoy_gt_0", REVENUE_MIN_YOY),
                               ("robustness_rev_yoy_gt_20", 20.0)):
        print(f"building panel: {label} ...", flush=True)
        panel = build_panel(args.snapshot, minimum_yoy=minimum_yoy,
                            horizon=PRIMARY_HORIZON)
        if panel.empty:
            report["specifications"][label] = {"months": 0}
            continue
        report["specifications"][label] = {
            "breadth": breadth(panel),
            "size_controlled": conditional_regression(panel, controls=True),
            "uncontrolled": conditional_regression(panel, controls=False),
            "two_by_two": two_by_two(panel),
        }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                      default=str) + "\n", encoding="utf-8")
    print(f"\nwritten: {args.output}")


if __name__ == "__main__":
    main()
