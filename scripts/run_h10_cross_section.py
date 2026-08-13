"""H10 Phase 3：布林候選之間的橫斷面可預測性。

登記：`research/HYPOTHESES.md` H10
流程：`docs/RESEARCH_V2_PIPELINE.md` §2 Phase 3、§2.1

**H10 是新假說，不是 H01 的復活。** H01（B1 之後平均會漲）維持否決。
H10 問的是：同一天觸發的候選之間，能不能事前分辨出哪幾檔比較好。

五個因子在 B1（進場決策時點）全部可得。pullback depth／days **不可用**——
那兩個要到 B2 才觀察得到，而決策發生在 B1。

本輪固定 8 次試驗：5 單因子 + 3 baseline。不得掃描因子參數。
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
from research.bollinger import bollinger_bands, setup_conditions, weekly_regime  # noqa: E402
from research.cross_section import (  # noqa: E402
    cross_sectional_rank, equal_weight_score, monotonicity, quantile_profile,
    random_selection_baseline, rank_ic, top_k_selection_return,
)
from research.event_study import forward_excess_returns  # noqa: E402
import scripts.run_h01_bollinger_event_study as H01  # noqa: E402

FACTORS = ["rs60", "clv", "breakout_strength", "pv_efficiency", "inst_net"]
TARGET_WINDOWS = (20, 60)
TOP_K = 10


def first_breaks(close: pd.Series, volume: pd.Series) -> list:
    close = close.dropna()
    if len(close) < 80:
        return []
    volume = volume.reindex(close.index)
    _, upper, _ = bollinger_bands(close)
    expanded, volume_ok = setup_conditions(close, volume)
    outside = (close > upper).fillna(False)
    fresh = outside & ~outside.shift(1, fill_value=False)
    return list(close.index[(fresh & expanded & volume_ok).to_numpy()])


def build_factors(wide, inst_net):
    """全部只用事件日**當日與之前**的資料，無前視。"""
    close, high, low, volume = (wide["close"], wide["high"], wide["low"], wide["volume"])
    _, upper, _ = bollinger_bands(close)
    true_range = pd.concat([
        (high - low),
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ]).groupby(level=0).max()
    atr = true_range.rolling(20).mean()
    daily_return = close / close.shift(1) - 1.0
    volume_ratio = volume / volume.rolling(20).mean()
    return {
        "rs60": (close / close.shift(60) - 1.0).rank(axis=1, pct=True),
        "clv": ((close - low) / (high - low).replace(0, np.nan)),
        "breakout_strength": (close - upper) / atr.replace(0, np.nan),
        "pv_efficiency": daily_return / volume_ratio.replace(0, np.nan),
        "inst_net": inst_net,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "research/results/h10_cross_section.json")
    parser.add_argument("--draws", type=int, default=1000)
    args = parser.parse_args()

    prices = pd.read_parquet(
        args.snapshot / "prices.parquet",
        columns=["stock_id", "trade_date", "open", "high", "low", "close",
                 "volume", "turnover"])
    prices["stock_id"] = prices["stock_id"].astype(str)
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    wide = {n: prices.pivot(index="trade_date", columns="stock_id", values=n).sort_index()
            for n in ("open", "high", "low", "close", "volume", "turnover")}

    dividends = pd.read_parquet(args.snapshot / "dividend_events.parquet")
    dividends["stock_id"] = dividends["stock_id"].astype(str)
    dividends["ex_date"] = pd.to_datetime(dividends["ex_date"])
    adjusted = apply_total_return_adjustment(wide["close"].apply(split_adjust), dividends)
    mask = H01.eligible_mask(wide, adjusted)
    regime = weekly_regime(wide["close"])
    benchmark = H01.equal_weight_benchmark(adjusted, mask)

    inst = pd.read_parquet(args.snapshot / "institutional.parquet",
                           columns=["stock_id", "trade_date", "foreign_net", "invest_net"])
    inst["stock_id"] = inst["stock_id"].astype(str)
    inst["trade_date"] = pd.to_datetime(inst["trade_date"])
    inst["net"] = inst["foreign_net"].fillna(0) + inst["invest_net"].fillna(0)
    inst_net = inst.pivot(index="trade_date", columns="stock_id",
                          values="net").reindex_like(wide["close"])

    factors = build_factors(wide, inst_net)

    rows = [(s, d) for s in wide["close"].columns
            for d in first_breaks(wide["close"][s], wide["volume"][s])]
    events = pd.DataFrame(rows, columns=["stock_id", "event_date"])
    keep = [(s in mask.columns and d in mask.index and bool(mask.at[d, s])
             and s in regime.columns and bool(regime.at[d, s]))
            for s, d in zip(events["stock_id"], events["event_date"])]
    events = events[pd.Series(keep, index=events.index)].reset_index(drop=True)
    print(f"B1 事件（通過濾網）: {len(events):,}", flush=True)

    per_event = forward_excess_returns(events=events, open_prices=wide["open"],
                                       benchmark_nav=benchmark, windows=TARGET_WINDOWS)
    for name, matrix in factors.items():
        per_event[name] = [
            matrix.at[d, s] if (d in matrix.index and s in matrix.columns) else np.nan
            for s, d in zip(per_event["stock_id"], per_event["event_date"])
        ]
    rank_columns = []
    for name in FACTORS:
        column = f"{name}_rank"
        per_event[column] = cross_sectional_rank(per_event, name)
        rank_columns.append(column)
    per_event["score"] = equal_weight_score(per_event, rank_columns)

    report = {
        "study": "H10 Phase 3 cross-sectional ranking among Bollinger candidates",
        "note": ("H01 remains rejected; H10 is a separate hypothesis about "
                 "cross-sectional predictability, not a rehabilitation"),
        "events": int(len(per_event)),
        "top_k": TOP_K,
        "trials_registered": 8,
        "factors": {},
        "baselines": {},
    }

    for window in TARGET_WINDOWS:
        target = f"car_{window}"
        for name in FACTORS:
            column = f"{name}_rank"
            profile = quantile_profile(per_event, column, target)
            entry = {
                "quantiles": [
                    {"q": int(r["quantile"]), "mean_pct": round(r["mean"] * 100, 4),
                     "median_pct": round(r["median"] * 100, 4), "n": int(r["count"])}
                    for _, r in profile.iterrows()
                ],
                "monotonicity": {k: (round(v, 5) if isinstance(v, float) else v)
                                 for k, v in monotonicity(profile).items()},
                "rank_ic": {k: (round(v, 5) if isinstance(v, float) else v)
                            for k, v in rank_ic(per_event, column, target).items()},
            }
            report["factors"].setdefault(name, {})[f"car_{window}"] = entry

        usable = per_event.dropna(subset=[target])
        random_stats = random_selection_baseline(usable, target, k=TOP_K,
                                                 draws=args.draws)
        full = top_k_selection_return(usable, "score", target, k=TOP_K)
        signal_only = top_k_selection_return(usable, "breakout_strength_rank",
                                             target, k=TOP_K)
        report["baselines"][f"car_{window}"] = {
            "random_mean_pct": round(random_stats["mean"] * 100, 4),
            "random_p05_pct": round(random_stats["p05"] * 100, 4),
            "random_p95_pct": round(random_stats["p95"] * 100, 4),
            "signal_only_mean_pct": round(float(signal_only.mean()) * 100, 4),
            "full_ranking_mean_pct": round(float(full.mean()) * 100, 4),
            "dates": int(len(full)),
        }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(f"written: {args.output}")


if __name__ == "__main__":
    main()
