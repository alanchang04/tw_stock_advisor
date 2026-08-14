"""量測「月末決策」這個慣例丟掉了多少營收反應（**EXPLORATORY 診斷**）。

## 這在回答什麼

月營收的法定申報期限是**次月 10 日前**。但 H11／H12／H16 用的決策日是
**次月月末**，比期限晚約 20 天。中間那段的價格反應，我們整條鏈都沒有算到。

    次月 1 日 ──────── 10 日 ──────────────── 月末
                    法定期限              我們的決策日
                    （資訊已完整）        （晚了約 20 天）
                        └──── 這段被丟掉了 ────┘

**這個腳本量的就是被丟掉那段。** 它是一個**描述統計**，不是假說檢定：

    E[ Rev+ 股票從「10 日後第一個交易日」到「月末」的超額報酬 ]

因此**不新增試驗次數**——它沒有檢定任何假說，只是告訴我們
「現行慣例每個月平均丟掉多少」。

## 為什麼不直接改成 10 日就好

改決策日會改變 H11／H12／H16 的凍結定義，那是**新規格**，
且會讓 forward journal 的定義指紋失效（依 M18 要另開 journal）。
所以正確順序是：**先量它值不值得**，再決定要不要為此立新規格。

## 為什麼「10 日」不構成前視

法定期限是次月 10 日前，因此在 10 日收盤時全體都已申報完畢。
本腳本一律取「10 日之後的第一個交易日」，方向保守。
`scripts/run_h04_h06_event_study.py` 早就用這個慣例，是 H11 鏈自己走偏了。
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
import scripts.run_h04_h06_event_study as H04  # noqa: E402
import scripts.run_h12_institutional_confirmation as H12  # noqa: E402

STATUTORY_DEADLINE_DAY = 10        # 次月 10 日前申報（法定）
NEWEY_WEST_LAGS = 6


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "reports/revenue_disclosure_window.json")
    args = parser.parse_args()

    prices = pd.read_parquet(args.snapshot / "prices.parquet",
                             columns=["stock_id", "trade_date", "close", "turnover"])
    prices["stock_id"] = prices["stock_id"].astype(str)
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    closes = prices.pivot(index="trade_date", columns="stock_id", values="close").sort_index()
    turnover = prices.pivot(index="trade_date", columns="stock_id", values="turnover").sort_index()

    dividends = pd.read_parquet(args.snapshot / "dividend_events.parquet")
    dividends["stock_id"] = dividends["stock_id"].astype(str)
    dividends["ex_date"] = pd.to_datetime(dividends["ex_date"])
    adjusted = apply_total_return_adjustment(closes.apply(split_adjust), dividends)

    mask = H04.liquid_universe_mask(closes, turnover)
    sessions = pd.DatetimeIndex(closes.index)
    H12.revenue_positive.cache = H12.load_revenue(args.snapshot)

    month_ends = H12.monthly_decision_dates(sessions, sessions[0], sessions[-1])

    rows = []
    for month_end in month_ends:
        period = month_end.to_period("M")
        # 「10 日後第一個交易日」——與 run_h04_h06 同一個慣例
        deadline = period.to_timestamp(how="start") + pd.Timedelta(days=STATUTORY_DEADLINE_DAY - 1)
        position = sessions.searchsorted(deadline, side="left")
        if position >= len(sessions):
            continue
        early = sessions[position]
        if early >= month_end:
            continue

        eligible = [s for s in closes.columns if bool(mask.at[early, s])]
        winners = sorted(H12.revenue_positive(args.snapshot, month_end, 0.0)
                         & set(eligible))
        strong = sorted(H12.revenue_positive(args.snapshot, month_end, 20.0)
                        & set(eligible))
        if len(winners) < 30:
            continue

        step = adjusted.loc[month_end] / adjusted.loc[early] - 1.0
        universe = float(step.reindex(eligible).mean(skipna=True))

        # 對照窗：月末之後「同樣長度」的一段。用來分辨
        # 「被丟掉的那段特別強」還是「整個公布後期間都在漂移」。
        span = sessions.get_loc(month_end) - position
        after_end = sessions.get_loc(month_end) + span
        following = np.nan
        if after_end < len(sessions):
            later = adjusted.iloc[after_end] / adjusted.loc[month_end] - 1.0
            following = (float(later.reindex(winners).mean(skipna=True))
                         - float(later.reindex(eligible).mean(skipna=True)))

        rows.append({
            "next_equal_span_excess": following,
            "period": str(period),
            "deadline_session": str(early.date()),
            "month_end_session": str(month_end.date()),
            "discarded_sessions": int(sessions.get_loc(month_end) - position),
            "rev_plus_excess": float(step.reindex(winners).mean(skipna=True)) - universe,
            "rev_strong_excess": (float(step.reindex(strong).mean(skipna=True)) - universe
                                  if len(strong) >= 30 else np.nan),
            "universe_return": universe,
        })

    frame = pd.DataFrame(rows)
    if frame.empty:
        raise SystemExit("沒有可用的月份")

    def summarise(column: str) -> dict:
        series = frame[column].dropna()
        se = newey_west_se(series, lags=NEWEY_WEST_LAGS)
        return {
            "months": int(series.size),
            "mean_pct": round(float(series.mean()) * 100, 4),
            "t_stat": round(float(series.mean() / se), 3)
            if se and np.isfinite(se) else None,
            "share_of_months_positive": round(float((series > 0).mean()), 4),
        }

    report = {
        "study": "how much revenue reaction the month-end decision convention discards",
        "status": "EXPLORATORY descriptive statistic — no hypothesis tested, no new trials",
        "question": ("H11/H12/H16 set the decision date at the month end of the "
                     "disclosure month, but the statutory filing deadline is the 10th. "
                     "How much excess return happens in the discarded window?"),
        "convention_in_use": {
            "h04_h06_event_study": "first session on/after the 10th (correct)",
            "h11_h12_h16_chain": "month end of the disclosure month (~20 days later)",
        },
        "no_look_ahead": ("the statutory deadline is the 10th, so by that session every "
                          "company has filed; the early anchor is the first session "
                          "on/after the 10th, which is conservative"),
        "discarded_sessions": {
            "median": int(frame["discarded_sessions"].median()),
            "min": int(frame["discarded_sessions"].min()),
            "max": int(frame["discarded_sessions"].max()),
        },
        "discarded_excess_return": {
            "rev_yoy_gt_0": summarise("rev_plus_excess"),
            "rev_yoy_gt_20": summarise("rev_strong_excess"),
        },
        "comparison_window_after_month_end": {
            "note": ("same number of sessions immediately AFTER the month end; "
                     "distinguishes 'the discarded window is special' from "
                     "'the whole post-disclosure period drifts'"),
            "rev_yoy_gt_0": summarise("next_equal_span_excess"),
        },
        "monthly": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")

    print(f"月數 {len(frame)}；每月丟掉的交易日中位數 "
          f"{int(frame['discarded_sessions'].median())} 日")
    for key, label in (("rev_yoy_gt_0", "Rev+ (yoy>0)"), ("rev_yoy_gt_20", "Rev+ (yoy>20%)")):
        block = report["discarded_excess_return"][key]
        print(f"  {label:18s} 被丟掉的超額報酬 {block['mean_pct']:+.4f}%／月  "
              f"t={block['t_stat']}  為正的月份 {block['share_of_months_positive']:.1%}")
    cmp_block = report["comparison_window_after_month_end"]["rev_yoy_gt_0"]
    print(f"  {'對照（月末後同長）':16s} {cmp_block['mean_pct']:+.4f}%  "
          f"t={cmp_block['t_stat']}  為正的月份 {cmp_block['share_of_months_positive']:.1%}")
    print(f"\nwritten: {args.output}")


if __name__ == "__main__":
    main()
