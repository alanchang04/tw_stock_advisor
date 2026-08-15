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
from research.fama_macbeth import _cross_sectional_slope, newey_west_se  # noqa: E402
from research.information_clock import earliest_executable_session  # noqa: E402
import scripts.run_h04_h06_event_study as H04  # noqa: E402
import scripts.run_h12_institutional_confirmation as H12  # noqa: E402

NEWEY_WEST_LAGS = 6


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "reports/revenue_disclosure_window.json")
    args = parser.parse_args()

    prices = pd.read_parquet(
        args.snapshot / "prices.parquet",
        columns=["stock_id", "trade_date", "open", "close", "turnover"])
    prices["stock_id"] = prices["stock_id"].astype(str)
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    closes = prices.pivot(index="trade_date", columns="stock_id", values="close").sort_index()
    opens = prices.pivot(index="trade_date", columns="stock_id", values="open").sort_index()
    turnover = prices.pivot(index="trade_date", columns="stock_id", values="turnover").sort_index()

    dividends = pd.read_parquet(args.snapshot / "dividend_events.parquet")
    dividends["stock_id"] = dividends["stock_id"].astype(str)
    dividends["ex_date"] = pd.to_datetime(dividends["ex_date"])
    adjusted = apply_total_return_adjustment(closes.apply(split_adjust), dividends)

    # 還原後的**開盤**價。M23 的可執行時點是「10 日之後第一個交易日的開盤」，
    # 因此進場價必須是開盤而不是收盤。同一天的 open/close 比值不受除權息與
    # 分割影響（兩者是同一個交易日的原始價），所以可以直接套到還原收盤上。
    adjusted_open = adjusted * (opens / closes.where(closes > 0))

    mask = H04.liquid_universe_mask(closes, turnover)
    sessions = pd.DatetimeIndex(closes.index)
    H12.revenue_positive.cache = H12.load_revenue(args.snapshot)
    # H11／H12 的規模控制：決策日當下的 20 日平均成交值取對數。
    # 這裡取「10 日後第一個交易日」，也就是本窗自己的決策點，與 H11 在
    # 月末取值的慣例一致（兩者都是在組合形成當下取控制變數）。
    log_size = np.log1p(turnover.rolling(20).mean())

    month_ends = H12.monthly_decision_dates(sessions, sessions[0], sessions[-1])

    rows = []
    for month_end in month_ends:
        # 決策月是 month_end 所在月，資料月是它的前一個月（同 H12 的對應）。
        # 可執行時點由 M23 時鐘統一決定：嚴格晚於次月 10 日的第一個交易日。
        period = month_end.to_period("M")
        early = earliest_executable_session("monthly_revenue", period - 1, sessions)
        if early is None or early >= month_end:
            continue
        position = sessions.get_loc(early)

        eligible = [s for s in closes.columns if bool(mask.at[early, s])]
        winners = sorted(H12.revenue_positive(args.snapshot, month_end, 0.0)
                         & set(eligible))
        strong = sorted(H12.revenue_positive(args.snapshot, month_end, 20.0)
                        & set(eligible))
        if len(winners) < 30:
            continue

        # 實際可執行的窗：在 early 這天**開盤**買進，月末收盤賣出。
        step = adjusted.loc[month_end] / adjusted_open.loc[early] - 1.0
        universe = float(step.reindex(eligible).mean(skipna=True))
        # 對照：從 early **收盤**起算。兩者的差就是進場當日的盤中那段——
        # 它是可執行的，但用收盤價量測會把它漏掉。
        from_close = adjusted.loc[month_end] / adjusted.loc[early] - 1.0
        close_anchor = (float(from_close.reindex(winners).mean(skipna=True))
                        - float(from_close.reindex(eligible).mean(skipna=True)))

        # 對照窗：月末之後「同樣長度」的一段。用來分辨
        # 「被丟掉的那段特別強」還是「整個公布後期間都在漂移」。
        span = sessions.get_loc(month_end) - position
        after_end = sessions.get_loc(month_end) + span
        following = np.nan
        if after_end < len(sessions):
            later = adjusted.iloc[after_end] / adjusted.loc[month_end] - 1.0
            following = (float(later.reindex(winners).mean(skipna=True))
                         - float(later.reindex(eligible).mean(skipna=True)))

        # ── 同口徑檢查：用 H11 的估計式再算一次 ──────────────────────
        # H11 報的是「Rev+ dummy 的橫斷面迴歸係數，控制 log_size」，
        # 也就是 Rev+ 相對 Rev− 的斜率；本腳本上面算的是 Rev+ 相對
        # 全體 universe 的 long-only 超額。兩者差一個 (1-p) 因子
        # （p = Rev+ 佔比），不能直接跟 H11 的 +0.936 併排比較。
        # 這裡把兩種都算出來，讓報告自己說明是哪一種。
        losers = [s for s in eligible if s not in set(winners)]
        dummy = pd.Series(0.0, index=eligible)
        dummy.loc[winners] = 1.0
        panel = pd.DataFrame({
            "signal": dummy,
            "target": step.reindex(eligible),
            "log_size": log_size.loc[early, eligible],
        }).dropna()
        slope_raw = _cross_sectional_slope(
            panel["signal"].to_numpy(), panel["target"].to_numpy())
        slope_size = _cross_sectional_slope(
            panel["signal"].to_numpy(), panel["target"].to_numpy(),
            panel[["log_size"]].to_numpy())

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
            "rev_plus_share": len(winners) / len(eligible) if eligible else np.nan,
            "rev_plus_excess_from_close": close_anchor,
            "fm_slope_raw": slope_raw,
            "fm_slope_size_controlled": slope_size,
            "rev_minus_mean": (float(step.reindex(losers).mean(skipna=True))
                               if len(losers) >= 30 else np.nan),
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
        "estimand": {
            "rev_plus_excess": ("mean(Rev+ return) - mean(eligible universe return); "
                                "long-only excess vs the equal-weight eligible universe. "
                                "NOT directly comparable to the H11 headline numbers."),
            "fm_slope_raw": ("cross-sectional OLS slope of the Rev+ dummy; equals "
                             "mean(Rev+) - mean(Rev-). This IS the H11 estimand "
                             "without controls."),
            "fm_slope_size_controlled": ("same slope controlling log(20d mean turnover) "
                                         "measured at the window's own decision session. "
                                         "This IS the H11 headline estimand."),
            "universe_return": ("mean return of the eligible universe over the same window; "
                                "reported so the reader can see how much of the raw Rev+ "
                                "return is just market drift"),
        },
        "entry_price_convention": {
            "used": ("open of the first session strictly after the statutory deadline "
                     "— this is what M23 says is executable"),
            "rev_plus_excess_from_close": summarise("rev_plus_excess_from_close"),
            "note": ("the from_close variant anchors on the same session's CLOSE and is "
                     "therefore too conservative: it discards that session's intraday "
                     "move, which is executable. The gap between the two is that "
                     "intraday segment."),
        },
        "market_drift_in_window": summarise("universe_return"),
        "rev_plus_share_of_universe": round(float(frame["rev_plus_share"].mean()), 4),
        "discarded_excess_return": {
            "rev_yoy_gt_0": summarise("rev_plus_excess"),
            "rev_yoy_gt_20": summarise("rev_strong_excess"),
        },
        "discarded_same_estimand_as_h11": {
            "note": ("H11 reports size-controlled Fama-MacBeth slopes "
                     "(+0.936 at M+1, +2.44% cumulative over 6M). The numbers here "
                     "are computed the same way over the discarded window, so they "
                     "can be placed on the same response path."),
            "fm_slope_raw": summarise("fm_slope_raw"),
            "fm_slope_size_controlled": summarise("fm_slope_size_controlled"),
            "rev_minus_mean": summarise("rev_minus_mean"),
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

    drift = report["market_drift_in_window"]
    print(f"\n同期 universe（全體合格等權）本身 {drift['mean_pct']:+.4f}%／月 "
          f"t={drift['t_stat']}  ← 上面的數字已經把它減掉了")
    print(f"Rev+ 佔合格股票 {report['rev_plus_share_of_universe']:.1%}")
    print("\n改用 H11 的估計式（Rev+ dummy 橫斷面斜率）重算同一段窗：")
    for key, label in (("fm_slope_raw", "FM 斜率（無控制）"),
                       ("fm_slope_size_controlled", "FM 斜率（控制規模）")):
        block = report["discarded_same_estimand_as_h11"][key]
        print(f"  {label:22s} {block['mean_pct']:+.4f}%  t={block['t_stat']}  "
              f"為正的月份 {block['share_of_months_positive']:.1%}")
    print(f"\nwritten: {args.output}")


if __name__ == "__main__":
    main()
