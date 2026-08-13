"""H04~H06 否證測試：現行策略殘存因子的 Phase 2 事件研究。

登記：`research/HYPOTHESES.md` H04~H06
規格：`docs/RESEARCH_V2_PIPELINE.md` §2 Phase 2

**這是否證測試，不是驗收。** 三個因子的假說來自 2026-07-19 的 P1 因子研究
（2015~2026 資料），依 PIPELINE §3.1 決策樹該段資料已污染；而乾淨期間
（2005~2014）根本沒有月營收與法人資料。因此：

| 結果 | 可以宣稱 | 不可以宣稱 |
|---|---|---|
| 效果消失 | 該因子被否證，正式除役 | — |
| 效果存活 | 「在正確方法下未被否證」 | ❌「有效」 |

修正舊 P1 研究的兩項會系統性高估顯著性的缺陷：自 t+1 開盤起算（舊報告從
事件日收盤起算，但因子收盤後才知道）、以及 block bootstrap（舊報告把重疊的
forward return 當獨立樣本算 t-stat）。

四次試驗：H04a（rev_yoy > 0）、H04b（rev_yoy > 20%）、H05（投信連買）、
H06（投信新進場）。須計入累計 n_trials。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.strategy import total_return_adjust  # noqa: E402
from research.event_study import run_event_study  # noqa: E402

# 觀察窗涵蓋現行策略的實際持有分布（平均 39.9、中位 25、p95 110 交易日）
STUDY_WINDOWS = (5, 10, 20, 40, 60, 120)

BENCHMARK = "0050"
# 沿用 agent/strategy.py 的**實際**定義：連續買超且累計量體 >= 100 張。
# 2026-08-13 第一版誤用「連續 3 日」的簡化版，未含量體門檻，已更正。
MIN_INVEST_STREAK_LOTS = 100
MIN_PRICE = 10.0
LIQUIDITY_WINDOW = 20


def load_panel(snapshot: Path):
    prices = pd.read_parquet(snapshot / "prices.parquet",
                             columns=["stock_id", "trade_date", "open", "close", "turnover"])
    prices["stock_id"] = prices["stock_id"].astype(str)
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    opens = prices.pivot(index="trade_date", columns="stock_id", values="open").sort_index()
    closes = prices.pivot(index="trade_date", columns="stock_id", values="close").sort_index()
    turnover = prices.pivot(index="trade_date", columns="stock_id", values="turnover").sort_index()
    return opens, closes, turnover


def benchmark_total_return(closes: pd.DataFrame, snapshot: Path) -> pd.Series:
    """0050 含息 NAV。用既有的 total_return_adjust，不另造一套還原邏輯。"""
    if BENCHMARK not in closes.columns:
        raise ValueError(f"快照缺少基準 {BENCHMARK}")
    dividends = pd.read_parquet(snapshot / "dividend_events.parquet")
    dividends["stock_id"] = dividends["stock_id"].astype(str)
    dividends["ex_date"] = pd.to_datetime(dividends["ex_date"])
    own = dividends[dividends["stock_id"] == BENCHMARK]
    return total_return_adjust(closes[BENCHMARK], own)


def liquid_universe_mask(closes: pd.DataFrame, turnover: pd.DataFrame) -> pd.DataFrame:
    """價格 >= 10 元且近 20 日均成交金額位於當日前 50%。與 MOM-1 §7.1 同口徑。"""
    liquidity = turnover.rolling(LIQUIDITY_WINDOW).mean()
    threshold = liquidity.median(axis=1)
    return (closes >= MIN_PRICE) & liquidity.ge(threshold, axis=0)


def revenue_events(snapshot: Path, sessions: pd.DatetimeIndex,
                   mask: pd.DataFrame, minimum_yoy: float) -> pd.DataFrame:
    """月營收事件。公布日採「次月 10 日後的第一個交易日」保守估計。

    官方規定次月 10 日前公布，但逐檔實際公布日不在快照內；取 10 日之後可確保
    **不會用到尚未公布的資訊**，方向保守。
    """
    revenue = pd.read_parquet(snapshot / "monthly_revenue.parquet")
    revenue["stock_id"] = revenue["stock_id"].astype(str)
    month = pd.PeriodIndex(revenue["year_month"].astype(str), freq="M")
    disclose = (month + 1).to_timestamp() + pd.Timedelta(days=9)
    revenue = revenue.assign(disclose=disclose)
    revenue = revenue[revenue["yoy_pct"].notna() & (revenue["yoy_pct"] > minimum_yoy)]

    positions = sessions.searchsorted(revenue["disclose"].to_numpy(), side="left")
    keep = positions < len(sessions)
    frame = pd.DataFrame({
        "stock_id": revenue["stock_id"].to_numpy()[keep],
        "event_date": sessions[positions[keep]],
    })
    return filter_by_mask(frame, mask)


def trend_stack_events(closes: pd.DataFrame, mask: pd.DataFrame) -> pd.DataFrame:
    """H09 多頭排列成立首日：MA5 > MA20 > MA60 由不成立轉為成立的那一天。

    沿用 `agent/strategy.py::_trend_matrices` 的定義（stack_days 由此累計）。
    """
    ma5, ma20, ma60 = (closes.rolling(w).mean() for w in (5, 20, 60))
    stack = ((ma5 > ma20) & (ma20 > ma60)).fillna(False)
    trigger = stack & ~stack.shift(1, fill_value=False)
    return stacked_to_events(trigger, mask)


def stacked_to_events(trigger: pd.DataFrame, mask: pd.DataFrame) -> pd.DataFrame:
    stacked = trigger.stack()
    hits = stacked[stacked].index
    frame = pd.DataFrame({
        "event_date": [d for d, _ in hits],
        "stock_id": [s for _, s in hits],
    })
    return filter_by_mask(frame, mask)


def institutional_events(snapshot: Path, mask: pd.DataFrame, kind: str) -> pd.DataFrame:
    """投信連買／新進場，或外資買超首日。"""
    column = "foreign_net" if kind == "foreign_buy" else "invest_net"
    inst = pd.read_parquet(snapshot / "institutional.parquet",
                           columns=["stock_id", "trade_date", column])
    inst["stock_id"] = inst["stock_id"].astype(str)
    inst["trade_date"] = pd.to_datetime(inst["trade_date"])
    wide = inst.pivot(index="trade_date", columns="stock_id",
                      values=column).sort_index()
    buying = wide > 0

    if kind == "foreign_buy":
        # 外資由未買超轉為買超的首日
        trigger = buying & ~buying.shift(1, fill_value=False)
    elif kind == "streak":
        # agent/strategy.py::_trend_matrices 的實際定義：連續買超期間的累計張數
        # 達到 MIN_INVEST_STREAK_LOTS 才算合格；事件日＝首次達標那一天。
        positive = wide.where(buying, 0.0)
        cumulative = positive.cumsum()
        reset = cumulative.where(~buying).ffill().fillna(0.0)
        streak_lots = (cumulative - reset).where(buying, 0.0) / 1000.0
        qualified = streak_lots >= MIN_INVEST_STREAK_LOTS
        trigger = qualified & ~qualified.shift(1, fill_value=False)
    elif kind == "new_entry":
        # 前 20 日都沒有買超，今日首次買超
        quiet = buying.rolling(20).sum().shift(1) == 0
        trigger = buying & quiet.fillna(False)
    else:
        raise ValueError(f"未知的 kind: {kind}")

    return stacked_to_events(trigger, mask)


def filter_by_mask(events: pd.DataFrame, mask: pd.DataFrame) -> pd.DataFrame:
    """只保留事件日通過 universe 條件者。"""
    if events.empty:
        return events
    aligned = mask.reindex(index=mask.index.union(events["event_date"].unique()))
    keep = []
    for stock_id, event_date in zip(events["stock_id"], events["event_date"]):
        if stock_id not in aligned.columns or event_date not in aligned.index:
            keep.append(False)
            continue
        keep.append(bool(aligned.at[event_date, stock_id]))
    return events[pd.Series(keep, index=events.index)].reset_index(drop=True)


def summarise(name: str, result) -> dict:
    return {
        "hypothesis": name,
        "events": result.events,
        "distinct_event_dates": result.distinct_event_dates,
        "stocks": result.stocks,
        "block_sessions": result.block_sessions,
        "bootstrap_draws": result.bootstrap_draws,
        "monotonic_car_path": result.is_monotonic_path(),
        "windows": {
            str(w): {
                "car_mean_pct": round(result.car_mean[w] * 100, 4),
                "car_median_pct": round(result.car_median[w] * 100, 4),
                "ci95_low_pct": round(result.ci_low[w] * 100, 4),
                "ci95_high_pct": round(result.ci_high[w] * 100, 4),
                "p_value": round(result.p_value[w], 4),
                "significant": bool(result.p_value[w] < 0.05),
            }
            for w in result.windows
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "research/results/h04_h06_event_study.json")
    parser.add_argument("--draws", type=int, default=1000)
    args = parser.parse_args()

    opens, closes, turnover = load_panel(args.snapshot)
    benchmark = benchmark_total_return(closes, args.snapshot)
    mask = liquid_universe_mask(closes, turnover)
    sessions = pd.DatetimeIndex(opens.index)

    definitions = {
        "H04a_rev_yoy_positive": lambda: revenue_events(args.snapshot, sessions, mask, 0.0),
        "H04b_rev_yoy_over_20": lambda: revenue_events(args.snapshot, sessions, mask, 20.0),
        "H05_invest_streak": lambda: institutional_events(args.snapshot, mask, "streak"),
        "H06_invest_new_entry": lambda: institutional_events(args.snapshot, mask, "new_entry"),
        "H07_foreign_buy": lambda: institutional_events(args.snapshot, mask, "foreign_buy"),
        "H09_trend_stack": lambda: trend_stack_events(closes, mask),
    }
    # H08 etf_accum 無法測試：它需要 DB 的 etf_changes 表，不在 parquet 快照內。
    # 依 PIPELINE §1 步驟 2，識別資料不存在即停，不以代理指標硬湊。

    report = {
        "study": "H04~H06 Phase 2 falsification test",
        "interpretation": (
            "contaminated data can falsify but cannot confirm; a surviving effect "
            "may only be reported as 'not falsified', never as 'effective'"
        ),
        "snapshot": str(args.snapshot),
        "benchmark": f"{BENCHMARK} total return",
        "entry_rule": "next session open after the event date",
        "return_type": "excess over benchmark",
        "windows": list(STUDY_WINDOWS),
        "not_tested": {
            "H08_etf_accum": "requires the etf_changes table in the operational DB; "
                             "absent from the parquet snapshot, so it is data-blocked "
                             "rather than falsified",
        },
        "trials_registered": 6,
        "results": [],
    }
    for name, builder in definitions.items():
        events = builder()
        print(f"{name}: {len(events):,} events", flush=True)
        if events.empty:
            report["results"].append({"hypothesis": name, "events": 0})
            continue
        result = run_event_study(events=events, open_prices=opens,
                                 benchmark_nav=benchmark, windows=STUDY_WINDOWS,
                                 draws=args.draws)
        report["results"].append(summarise(name, result))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps(report["results"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
