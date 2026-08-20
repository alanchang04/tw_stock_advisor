"""方法稽核：把 M1~M5／M9 套用到已有結論的三個事件集。

規範：`docs/RESEARCH_METHOD_AMENDMENTS.md`

**這不是新的假說測試，是對既有結論的重新檢驗。** 因此不新增試驗次數：
事件定義、觀察窗、濾網全部沿用原研究，一個都不改。改的只有推論方法。

受檢三個事件集，都是先前結論最需要被質疑的：

| 事件集 | 先前結論 | 要檢驗的懷疑 |
|---|---|---|
| H01 B1（布林首次突破） | 否決，但右尾極厚 | 右尾是不是同一段行情被重複計算（M3） |
| H04b 月營收 YoY>20% | 功效不足，無法判定 | 20 日 block 撐得起 120 日窗嗎（M1） |
| H05 投信連買 | 功效不足，無法判定 | 同上，另加基準選擇（M9） |

輸出 `research/results/method_audit.json`。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.strategy import apply_total_return_adjustment, split_adjust  # noqa: E402
from research.episodes import (  # noqa: E402
    deduplicate_overlapping_events, leave_one_out_audit, tail_breadth,
)
from research.event_study import forward_excess_returns  # noqa: E402
from research.inference import (  # noqa: E402
    MIN_TRADEABLE_EDGE, SENSITIVITY_BLOCKS, block_length_sensitivity,
    economic_significance, label_verdict, multiple_testing_budget,
    stationary_bootstrap_ci,
)
import scripts.run_h01_bollinger_event_study as H01  # noqa: E402
import scripts.run_h04_h06_event_study as H04  # noqa: E402
import scripts.run_h10_cross_section as H10  # noqa: E402

AUDIT_WINDOWS = (60, 120)
# 先前兩輪實際檢視過的格數：6 因子 × 6 窗（H04~H09）＋ 5 窗（H01 多側）
# ＋ 5 窗（H01 空側）＋ 5 因子 × 2 窗（H10）。看過就要算，這是 M2 的定義。
CELLS_EXAMINED = 6 * 6 + 5 + 5 + 5 * 2


def audit_one(name: str, events: pd.DataFrame, *, opens: pd.DataFrame,
              benchmarks: dict[str, pd.Series], draws: int) -> dict:
    sessions = pd.DatetimeIndex(opens.index)
    entry: dict = {"event_set": name, "raw_events": int(len(events)),
                   "raw_distinct_dates": int(events["event_date"].nunique()),
                   "benchmarks": {}}

    for benchmark_name, nav in benchmarks.items():
        per_benchmark: dict = {}
        for window in AUDIT_WINDOWS:
            column = f"car_{window}"

            # ── 原始（未去重）─────────────────────────────────────────
            raw = forward_excess_returns(events=events, open_prices=opens,
                                         benchmark_nav=nav, windows=(window,))
            raw = raw.dropna(subset=[column])
            if raw.empty:
                per_benchmark[str(window)] = {"events": 0}
                continue

            # ── M3：同一檔股票在觀察窗內重複觸發併為一個 episode ──────
            deduped = deduplicate_overlapping_events(
                raw, sessions=sessions, window_sessions=window)

            sensitivity = block_length_sensitivity(
                deduped, window, sessions=sessions,
                blocks=SENSITIVITY_BLOCKS, draws=draws)
            _, _, stationary_p = stationary_bootstrap_ci(
                deduped, window, sessions=sessions,
                mean_block_sessions=window, draws=draws)
            economics = economic_significance(
                deduped, window, sessions=sessions,
                block_sessions=window, draws=draws)

            raw_mean = float(raw[column].mean())
            dedup_mean = float(deduped[column].mean())
            per_benchmark[str(window)] = {
                "events_raw": int(len(raw)),
                "events_deduplicated": int(len(deduped)),
                "duplicate_share": round(1.0 - len(deduped) / len(raw), 4),
                "car_mean_raw_pct": round(raw_mean * 100, 4),
                "car_mean_deduplicated_pct": round(dedup_mean * 100, 4),
                "car_median_deduplicated_pct": round(
                    float(deduped[column].median()) * 100, 4),
                # M1：p 值對 block 長度有多敏感
                "block_sensitivity": {
                    str(b): round(v["p_value"], 4)
                    for b, v in sensitivity.by_block.items()
                },
                "p_range_across_blocks": round(sensitivity.p_range, 4),
                "verdict_stable_across_blocks": sensitivity.verdict_is_stable(),
                "stationary_bootstrap_p": round(stationary_p, 4),
                # M5：對經濟門檻而非對零檢定
                "economic_threshold_pct": round(MIN_TRADEABLE_EDGE * 100, 4),
                "exceeds_economic_threshold": economics["exceeds_threshold"],
                "p_vs_economic_threshold": round(economics["p_value"], 4),
                "verdict": label_verdict(
                    p_value=max(sensitivity.p_values), observed=dedup_mean),
                # M4：右尾廣度與逐組刪除
                "tail_raw": tail_breadth(raw, column),
                "tail_deduplicated": tail_breadth(deduped, column),
                "leave_one_year_out": leave_one_out_audit(
                    deduped, column, by="year"),
                "leave_one_stock_out": leave_one_out_audit(
                    deduped, column, by="stock_id"),
            }
        entry["benchmarks"][benchmark_name] = per_benchmark
    return entry


def bollinger_events(snapshot: Path, opens, closes, volume, turnover,
                     adjusted) -> pd.DataFrame:
    """H01 的 B1 事件，沿用 run_h10_cross_section 的濾網，一字不改。"""
    wide = {"open": opens, "close": closes, "volume": volume,
            "turnover": turnover, "high": None, "low": None}
    mask = H01.eligible_mask(wide, adjusted)
    regime = H10.weekly_regime(closes)
    rows = [(s, d) for s in closes.columns
            for d in H10.first_breaks(closes[s], volume[s])]
    events = pd.DataFrame(rows, columns=["stock_id", "event_date"])
    keep = [(s in mask.columns and d in mask.index and bool(mask.at[d, s])
             and s in regime.columns and bool(regime.at[d, s]))
            for s, d in zip(events["stock_id"], events["event_date"])]
    return events[pd.Series(keep, index=events.index)].reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "research/results/method_audit.json")
    parser.add_argument("--draws", type=int, default=500)
    args = parser.parse_args()

    prices = pd.read_parquet(
        args.snapshot / "prices.parquet",
        columns=["stock_id", "trade_date", "open", "close", "volume", "turnover"])
    prices["stock_id"] = prices["stock_id"].astype(str)
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    opens, closes, volume, turnover = (
        prices.pivot(index="trade_date", columns="stock_id", values=n).sort_index()
        for n in ("open", "close", "volume", "turnover"))

    dividends = pd.read_parquet(args.snapshot / "dividend_events.parquet")
    dividends["stock_id"] = dividends["stock_id"].astype(str)
    dividends["ex_date"] = pd.to_datetime(dividends["ex_date"])
    adjusted = apply_total_return_adjustment(closes.apply(split_adjust), dividends)

    # M9：兩個基準各回答一個不同的問題，不可混為一談。
    #   0050 含息   → 投資決策基準（「值不值得把錢放這裡」）
    #   等權 universe → 機制驗證基準（「這個訊號有沒有選股資訊」）
    # 月營收高成長股天生偏中小型，用市值加權的 0050 當唯一基準會把
    # 規模效應算進訊號效力。
    mask = H04.liquid_universe_mask(closes, turnover)
    benchmarks = {
        "0050_total_return": H04.benchmark_total_return(closes, args.snapshot),
        "equal_weight_universe": H01.equal_weight_benchmark(adjusted, mask),
    }
    sessions = pd.DatetimeIndex(opens.index)

    event_sets = {
        "H01_bollinger_b1": lambda: bollinger_events(
            args.snapshot, opens, closes, volume, turnover, adjusted),
        "H04b_rev_yoy_over_20": lambda: H04.revenue_events(
            args.snapshot, sessions, mask, 20.0),
        "H05_invest_streak": lambda: H04.institutional_events(
            args.snapshot, mask, "streak"),
    }

    report = {
        "study": "method audit of existing conclusions (M1-M5, M9)",
        "amendments": "docs/RESEARCH_METHOD_AMENDMENTS.md",
        "note": ("re-examination of existing event sets under corrected inference; "
                 "no event definition, window, or filter was changed, so this "
                 "adds no new trials to the cumulative n_trials count"),
        "windows": list(AUDIT_WINDOWS),
        "bootstrap_draws": args.draws,
        "new_trials": 0,
        "multiple_testing_budget": multiple_testing_budget(
            cells=CELLS_EXAMINED, significant=1),
        "results": [],
    }

    for name, builder in event_sets.items():
        events = builder()
        print(f"{name}: {len(events):,} events", flush=True)
        report["results"].append(audit_one(
            name, events, opens=opens, benchmarks=benchmarks, draws=args.draws))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                      default=str) + "\n", encoding="utf-8")
    print(f"\nwritten: {args.output}")


if __name__ == "__main__":
    main()
