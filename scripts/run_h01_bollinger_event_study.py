"""H01 布林二次突破 Phase 2 事件研究。

規格：`docs/SPEC_H01_BOLLINGER_REBREAKOUT.md`（v1.0，已凍結 2026-08-13）
登記：`research/HYPOTHESES.md` H01

Phase 2 只回答「事件之後相對大盤會不會漲」，**不含**停損、trailing、
部位大小、排序權重、成本。

依規格 §8 的三項裁決：

- §8.1 上下沿都測，但空側只作機制對稱性檢驗，不得宣稱可放空操作
- §8.2 **主檢定基準＝同日合格 universe 的等權總報酬**，並列報 0050 含息
- §8.3 H01 只用價量，納入 TPEX

試驗次數固定為 2（多側、空側），不得掃描任何參數。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.strategy import (  # noqa: E402
    apply_total_return_adjustment, split_adjust, total_return_adjust,
)
from research.bollinger import detect_all, weekly_regime  # noqa: E402
from research.event_study import DEFAULT_WINDOWS, run_event_study  # noqa: E402

MIN_PRICE = 10.0
MIN_HISTORY = 252
LIQUIDITY_WINDOW = 20


def load_inputs(snapshot: Path):
    prices = pd.read_parquet(
        snapshot / "prices.parquet",
        columns=["stock_id", "trade_date", "open", "close", "volume", "turnover"])
    prices["stock_id"] = prices["stock_id"].astype(str)
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    wide = {name: prices.pivot(index="trade_date", columns="stock_id", values=name).sort_index()
            for name in ("open", "close", "volume", "turnover")}
    dividends = pd.read_parquet(snapshot / "dividend_events.parquet")
    dividends["stock_id"] = dividends["stock_id"].astype(str)
    dividends["ex_date"] = pd.to_datetime(dividends["ex_date"])
    return wide, dividends


def eligible_mask(wide, adjusted_close):
    """規格 §2 的 universe。以四位數代號粗篩普通股。

    PIT master 只覆蓋 2005~2014，2015~2026 沒有對應元件，因此這裡以代號規則
    近似，並在報告中明確標示為近似而非 PIT——這是本次研究的已知限制。
    """
    closes = wide["close"]
    common = pd.Index([c for c in closes.columns if c.isdigit() and len(c) == 4])
    liquidity = wide["turnover"].rolling(LIQUIDITY_WINDOW).mean()
    history = adjusted_close.notna().cumsum()
    mask = (
        (closes >= MIN_PRICE)
        & (history >= MIN_HISTORY)
        & liquidity.ge(liquidity[common].median(axis=1), axis=0)
    )
    mask[[c for c in closes.columns if c not in common]] = False
    return mask


def equal_weight_benchmark(adjusted_close: pd.DataFrame, mask: pd.DataFrame) -> pd.Series:
    """同日合格 universe 的等權總報酬 NAV（規格 §8.2 的主檢定基準）。

    每日以當日合格股票的還原報酬取等權平均後累乘。這衡量的是「隨機挑一檔
    合格股票」的報酬，因此把 alpha 從規模效應中分離出來——0050 是市值加權
    且高度集中於台積電，用它當唯一基準會把規模效應混進訊號效力。
    """
    returns = adjusted_close.pct_change()
    eligible_returns = returns.where(mask.shift(1, fill_value=False))
    daily = eligible_returns.mean(axis=1, skipna=True).fillna(0.0)
    return (1.0 + daily).cumprod()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "research/results/h01_bollinger_event_study.json")
    parser.add_argument("--draws", type=int, default=1000)
    args = parser.parse_args()

    wide, dividends = load_inputs(args.snapshot)
    # 先還原分割再還原除權息。0050 在 2025-06-18 有一次 +74.78% 的分割斷點，
    # 且它不在 dividend_events 內；只做除權息還原會讓基準在該日出現假跳空，
    # 使任何跨越該日的窗格產生假的負超額。agent/backtest.py 同樣先 split_adjust。
    split_adjusted = wide["close"].apply(split_adjust)
    adjusted_close = apply_total_return_adjustment(split_adjusted, dividends)
    mask = eligible_mask(wide, adjusted_close)
    regime = weekly_regime(wide["close"])

    benchmarks = {
        "equal_weight_universe": equal_weight_benchmark(adjusted_close, mask),
        "0050_total_return": total_return_adjust(
            split_adjust(wide["close"]["0050"]),
            dividends[dividends["stock_id"] == "0050"]),
    }

    report = {
        "study": "H01 Bollinger re-breakout, Phase 2 event study",
        "spec": "docs/SPEC_H01_BOLLINGER_REBREAKOUT.md v1.0 (frozen 2026-08-13)",
        "primary_benchmark": "equal_weight_universe",
        "entry_rule": "next session open after the event date",
        "windows": list(DEFAULT_WINDOWS),
        "trials_registered": 2,
        "known_limitation": (
            "PIT security master covers 2005-2014 only; the 2015-2026 universe uses a "
            "4-digit ticker rule as an approximation, which is explicitly not PIT"
        ),
        "short_side_caveat": (
            "the lower-band result is a mechanism-symmetry check only and must never "
            "be read as a tradable short strategy; Taiwan borrowing costs are not modelled"
        ),
        "results": [],
    }

    for side, label in (("upper", "long_rebreakout"), ("lower", "short_rebreakout")):
        events = detect_all(wide["close"], wide["volume"], side=side)
        if events.empty:
            report["results"].append({"side": label, "events": 0})
            continue
        # 只保留事件日通過 universe 且週 K regime 同向者
        keep = []
        for stock_id, event_date in zip(events["stock_id"], events["event_date"]):
            if stock_id not in mask.columns or event_date not in mask.index:
                keep.append(False)
                continue
            eligible = bool(mask.at[event_date, stock_id])
            bull = bool(regime.at[event_date, stock_id]) if stock_id in regime.columns else False
            keep.append(eligible and (bull if side == "upper" else not bull))
        events = events[pd.Series(keep, index=events.index)].reset_index(drop=True)
        print(f"{label}: {len(events):,} events", flush=True)
        if events.empty:
            report["results"].append({"side": label, "events": 0})
            continue

        entry = {"side": label, "events": int(len(events)),
                 "median_periods_between_breaks": float(events["periods_between"].median()),
                 "benchmarks": {}}
        for name, nav in benchmarks.items():
            result = run_event_study(events=events, open_prices=wide["open"],
                                     benchmark_nav=nav, draws=args.draws)
            entry["distinct_event_dates"] = result.distinct_event_dates
            entry["stocks"] = result.stocks
            entry["benchmarks"][name] = {
                "monotonic_car_path": result.is_monotonic_path(),
                "windows": {
                    str(w): {
                        "car_mean_pct": round(result.car_mean[w] * 100, 4),
                        "car_median_pct": round(result.car_median[w] * 100, 4),
                        "ci95_low_pct": round(result.ci_low[w] * 100, 4),
                        "ci95_high_pct": round(result.ci_high[w] * 100, 4),
                        "p_value": round(result.p_value[w], 4),
                        "significant": bool(result.p_value[w] < 0.05),
                    } for w in result.windows
                },
            }
        report["results"].append(entry)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(f"\nwritten: {args.output}")


if __name__ == "__main__":
    main()
