"""P3-11: is the 投信 streak definition too brittle to be useful?

Pre-registered in research/EXPERIMENTS.md on 2026-08-08 before any run, including
both thresholds, which come from this project's own measured distribution rather
than from another context (the mistake that cost P3-10).

    V0_baseline     production definition
    V1_tolerance    a net sell <=10% of the streak's own average buy does not reset
    V2_size_floor   a day counts only when the buy is >=5% of that day's volume

Only `data["_inv_streak"]` is swapped; everything else -- pool construction,
scoring weights, stops, exits, market filter, costs -- is held at production
values, so any difference is attributable to the streak definition alone.

development only, TWSE-only universe.  validation and holdout are not read.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.backtest import _load
from agent.strategy import STRATEGY
from research.data_splits import slice_dates
from research.invest_streak_variants import (
    SIZE_FLOOR_PCT_OF_VOLUME,
    TOLERANCE_RATIO,
    qualifying_stock_days,
    streak_with_size_floor,
    streak_with_tolerance,
)
from research.provenance import environment_block
from research.qullamaggie import attach_unconditional_entry_paths
from scripts.run_qullamaggie_factorial import (
    PARQUET_DIR,
    build_entry_schedule,
    prepare_research_data,
    run_factorial_backtest,
)

OUT_JSON = ROOT / "research" / "results" / "invest_streak_2026-08-08.json"
UNIVERSE_MARKETS = ("TWSE",)
REBALANCE = 5
HORIZONS = (3, 5, 10, 20)

#: F0：上市宇宙 dev 基準（P3-9b 建立，P3-10 已再次精確重現）
BASELINE = {"annual_return": 18.36, "sharpe": 1.13, "mdd": -15.15, "trades": 225}


def _metrics(trades: pd.DataFrame) -> dict:
    attrs = trades.attrs
    pnl = pd.to_numeric(trades["net_pnl"], errors="coerce").fillna(0.0)
    ordered = pnl.sort_values(ascending=False)
    gross = float(pnl[pnl > 0].sum())
    by_year = defaultdict(float)
    for row in trades.itertuples(index=False):
        by_year[str(row.entry_date.year)] += float(row.net_pnl)
    out = {
        "annual_return": float(attrs["ann_ret"]), "sharpe": float(attrs["sharpe"]),
        "mdd": float(attrs["nav_mdd"]), "calmar": float(attrs["calmar"]),
        "trades": int(len(trades)), "win_rate": float((pnl > 0).mean()),
        "avg_hold_days": float(pd.to_numeric(trades["hold"], errors="coerce").mean()),
        "net_pnl_after_removing_best_5": float(ordered.iloc[5:].sum()),
        "max_year_profit_share": (max((v / gross for v in by_year.values() if v > 0),
                                      default=0.0) if gross > 0 else 0.0),
    }
    for h in HORIZONS:
        s = pd.to_numeric(trades.get(f"ret_day{h}"), errors="coerce").dropna()
        out[f"day{h}_mean"] = float(s.mean()) if len(s) else None
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    cfg = {**STRATEGY, "universe_markets": UNIVERSE_MARKETS}
    data = _load(parquet_dir=str(PARQUET_DIR))
    panels = prepare_research_data(data, cfg)
    closes = panels["closes"]

    # 兩個替代定義的輸入：張為單位，與 compute_factor_matrices 的內部口徑一致
    invest_lots = data["inst"].pivot_table(
        index="trade_date", columns="stock_id", values="invest_net", aggfunc="sum"
    ).reindex(index=closes.index, columns=closes.columns) / 1000.0
    volume_lots = panels["volume"].reindex_like(invest_lots) / 1000.0

    base_streak = data["_inv_streak"].copy()
    variants = {
        "V0_baseline": base_streak,
        "V1_tolerance": streak_with_tolerance(invest_lots),
        "V2_size_floor": streak_with_size_floor(invest_lots, volume_lots),
    }

    qual = {k: qualifying_stock_days(v.reindex_like(base_streak).fillna(0))
            for k, v in variants.items()}
    b = max(1, qual["V0_baseline"])
    treat = {
        "qualifying_stock_days": qual,
        "v1_change_vs_v0": qual["V1_tolerance"] / b - 1.0,
        "v2_change_vs_v0": qual["V2_size_floor"] / b - 1.0,
    }
    treat["v1_applied"] = treat["v1_change_vs_v0"] >= 0.20
    treat["v2_applied"] = treat["v2_change_vs_v0"] <= -0.20
    treat["passed"] = treat["v1_applied"] and treat["v2_applied"]
    print(f"合格 streak 股票日：{qual}", flush=True)
    print(f"V1 {treat['v1_change_vs_v0']:+.1%}  V2 {treat['v2_change_vs_v0']:+.1%}  "
          f"介入生效={treat['passed']}", flush=True)

    all_dates = sorted(pd.to_datetime(data["prices"]["trade_date"]).dt.date.unique())
    sel = slice_dates(all_dates, "development", purpose="P3-11 投信連買定義（容忍／量體）")
    lo, hi = min(sel), max(sel)
    dates = [d for d in sorted(closes.index) if lo <= d <= hi]

    results = {}
    for name, streak in variants.items():
        data["_inv_streak"] = streak.reindex_like(base_streak).fillna(0)
        schedule = build_entry_schedule(data, panels, dates, "current", cfg,
                                        rebalance=REBALANCE)
        trades = run_factorial_backtest(
            data=data, panels=panels, start_date=lo, end_date=hi,
            entry_mode="current", exit_mode="current", cfg=cfg,
            rebalance=REBALANCE, entry_schedule=schedule,
        )
        if trades.empty:
            raise RuntimeError(f"{name}: no trades")
        trades = attach_unconditional_entry_paths(trades, closes, horizons=HORIZONS)
        results[name] = _metrics(trades)
        results[name]["signal_days"] = len(schedule)
        m = results[name]
        print(f"{name:16s} ann={m['annual_return']:+.2%} S={m['sharpe']:.2f} "
              f"MDD={m['mdd']:+.2%} n={m['trades']} 訊號日={len(schedule)}", flush=True)
    data["_inv_streak"] = base_streak

    v0 = results["V0_baseline"]
    obs = {"annual_return": round(v0["annual_return"] * 100, 2),
           "sharpe": round(v0["sharpe"], 2),
           "mdd": round(v0["mdd"] * 100, 2), "trades": v0["trades"]}
    f0 = {"expected": BASELINE, "observed": obs, "passed": obs == BASELINE}

    verdicts = {}
    for name in ("V1_tolerance", "V2_size_floor"):
        m = results[name]
        checks = {
            "F1_beats_v0": bool(m["annual_return"] > v0["annual_return"]
                                and m["sharpe"] > v0["sharpe"]),
            "F2_mdd_ok": bool(m["mdd"] >= v0["mdd"] - 0.03),
            "F3_positive_without_best_5": bool(m["net_pnl_after_removing_best_5"] > 0),
            "F4_not_concentrated": bool(m["max_year_profit_share"] <= 0.40),
        }
        verdicts[name] = {**checks, "passes_all": all(checks.values()),
                          "max_year_profit_share": m["max_year_profit_share"]}

    payload = {
        "experiment": "invest_streak_definition_v1", "run_date": str(date.today()),
        "registered": "research/EXPERIMENTS.md P3-11（2026-08-08，跑之前登記）",
        "environment": environment_block(data, parquet_dir=str(PARQUET_DIR)),
        "spec": {"tolerance_ratio": TOLERANCE_RATIO,
                 "size_floor_pct_of_volume": SIZE_FLOOR_PCT_OF_VOLUME,
                 "source": "門檻取自 development/上市宇宙實測分布，非進口常數",
                 "universe_markets": list(UNIVERSE_MARKETS)},
        "prior_expectation": "V1 看好（斷點證據強）、V2 不看好（單日規模與報酬 rho=-0.01）",
        "development": {"start": str(lo), "end": str(hi), "variants": results},
        "treatment_check": treat,
        "assessment": {"F0_baseline": f0, "F0b_treatment_applied": treat,
                       "per_variant": verdicts,
                       "passing": [k for k, v in verdicts.items() if v["passes_all"]]},
        "validation_touched": False, "holdout_touched": False,
        "formal_defaults_changed": False,
    }
    if args.no_write:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    print(f"wrote {OUT_JSON}")


if __name__ == "__main__":
    main()
