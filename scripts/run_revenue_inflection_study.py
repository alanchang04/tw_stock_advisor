"""P3-12: should the revenue factors look at the inflection rather than the level?

Pre-registered in research/EXPERIMENTS.md on 2026-08-08 before any run.

`w_rev_yoy` (YoY > 0) and `w_rev_accel` (YoY > 20%) are both *levels*; together
they carry 4.0 of the scoring weight and neither looks at whether growth is
turning up.  The user's point is that by the time a stock shows two or three
months of YoY growth, the move has happened.

Raw month-on-month would flag every December -- Taiwanese monthly revenue is
strongly seasonal.  YoY is already seasonality-adjusted, so the inflection is
measured as **YoY(M) - YoY(M-1)**, which answers "is growth turning up" without
the calendar artefact.

    V0_baseline     quality top-20 -> top 5 by quality score
    V1_accel_gate   keep only YoY(M) > YoY(M-1) -> top 5 by quality score
    V2_accel_rank   re-sort by YoY acceleration -> top 5

Both variants are parameter-free.  development only, TWSE-only universe.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.backtest import _available_rev_month, _load
from agent.strategy import STRATEGY
from research.data_splits import slice_dates
from research.provenance import environment_block
from research.qullamaggie import attach_unconditional_entry_paths
from scripts.run_qullamaggie_factorial import (
    PARQUET_DIR, current_candidates, prepare_research_data, run_factorial_backtest,
)

OUT_JSON = ROOT / "research" / "results" / "revenue_inflection_2026-08-08.json"
UNIVERSE_MARKETS = ("TWSE",)
TOP_N, POOL_N, REBALANCE = 5, 20, 5
HORIZONS = (3, 5, 10, 20)
BASELINE = {"annual_return": 18.36, "sharpe": 1.13, "mdd": -15.15, "trades": 225}
VARIANTS = ("V0_baseline", "V1_accel_gate", "V2_accel_rank")


def _prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:])
    m -= 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y:04d}-{m:02d}"


def build_accel(data: dict) -> dict:
    """{(stock_id, year_month): YoY(M) - YoY(M-1)}，只用已公布月份組成。"""
    rev_map = data.get("rev_map") or {}
    accel = {}
    for (sid, ym), yoy in rev_map.items():
        if yoy is None or pd.isna(yoy):
            continue
        prev = rev_map.get((sid, _prev_month(ym)))
        if prev is None or pd.isna(prev):
            continue
        accel[(sid, ym)] = float(yoy) - float(prev)
    return accel


def build_schedules(data: dict, dates: list, cfg: dict, accel: dict):
    schedules = {v: {} for v in VARIANTS}
    stats = {"pool_names": 0, "gated_out": 0, "no_accel": 0,
             "overlap": 0, "rank_days": 0, "dropped": Counter()}

    for i, d in enumerate(dates):
        if i % REBALANCE != 0:
            continue
        pool = current_candidates(data, d, cfg, top_n=TOP_N)[:POOL_N]
        if not pool:
            continue
        ym = _available_rev_month(d)
        stats["pool_names"] += len(pool)
        schedules["V0_baseline"][d] = pool
        v0_head = pool[:TOP_N]

        kept, scored = [], []
        for sid in pool:
            a = accel.get((sid, ym))
            if a is None:
                stats["no_accel"] += 1
                continue
            scored.append((sid, a))
            if a > 0:
                kept.append(sid)
            else:
                stats["gated_out"] += 1
                stats["dropped"][sid] += 1
        if kept:
            schedules["V1_accel_gate"][d] = kept
        if scored:
            ranked = [s for s, _ in sorted(scored, key=lambda x: x[1], reverse=True)]
            schedules["V2_accel_rank"][d] = ranked
            stats["rank_days"] += 1
            stats["overlap"] += len(set(ranked[:TOP_N]) & set(v0_head))
        if i % 250 == 0:
            print(f"  pool {i}/{len(dates)} {d}", flush=True)
    return schedules, stats


def _metrics(trades: pd.DataFrame) -> dict:
    attrs = trades.attrs
    pnl = pd.to_numeric(trades["net_pnl"], errors="coerce").fillna(0.0)
    ordered = pnl.sort_values(ascending=False)
    gross = float(pnl[pnl > 0].sum())
    by_year = defaultdict(float)
    for r in trades.itertuples(index=False):
        by_year[str(r.entry_date.year)] += float(r.net_pnl)
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
    accel = build_accel(data)
    print(f"可用的營收加速度樣本：{len(accel):,} 個 (stock, month)", flush=True)

    all_dates = sorted(pd.to_datetime(data["prices"]["trade_date"]).dt.date.unique())
    sel = slice_dates(all_dates, "development", purpose="P3-12 營收拐點（加速度閘門／重排）")
    lo, hi = min(sel), max(sel)
    dates = [d for d in sorted(panels["closes"].index) if lo <= d <= hi]

    schedules, stats = build_schedules(data, dates, cfg, accel)
    n = max(1, stats["pool_names"])
    treat = {
        "gate_drop_rate": stats["gated_out"] / n,
        "no_accel_rate": stats["no_accel"] / n,
        "v2_overlap_with_v0": stats["overlap"] / max(1, stats["rank_days"] * TOP_N),
        "most_dropped": stats["dropped"].most_common(15),
    }
    treat["v1_applied"] = treat["gate_drop_rate"] >= 0.05
    treat["v2_applied"] = treat["v2_overlap_with_v0"] <= 0.80
    treat["passed"] = treat["v1_applied"] and treat["v2_applied"]
    print(f"介入：閘門剔除 {treat['gate_drop_rate']:.1%}、無加速度 {treat['no_accel_rate']:.1%}、"
          f"V2 重疊 {treat['v2_overlap_with_v0']:.1%}、生效={treat['passed']}", flush=True)

    results = {}
    for name in VARIANTS:
        trades = run_factorial_backtest(
            data=data, panels=panels, start_date=lo, end_date=hi,
            entry_mode="current", exit_mode="current", cfg=cfg,
            rebalance=REBALANCE, entry_schedule=schedules[name],
        )
        if trades.empty:
            raise RuntimeError(f"{name}: no trades")
        trades = attach_unconditional_entry_paths(trades, panels["closes"], horizons=HORIZONS)
        results[name] = _metrics(trades)
        m = results[name]
        print(f"{name:16s} ann={m['annual_return']:+.2%} S={m['sharpe']:.2f} "
              f"MDD={m['mdd']:+.2%} n={m['trades']}", flush=True)

    v0 = results["V0_baseline"]
    obs = {"annual_return": round(v0["annual_return"] * 100, 2),
           "sharpe": round(v0["sharpe"], 2),
           "mdd": round(v0["mdd"] * 100, 2), "trades": v0["trades"]}
    f0 = {"expected": BASELINE, "observed": obs, "passed": obs == BASELINE}

    verdicts = {}
    for name in ("V1_accel_gate", "V2_accel_rank"):
        m = results[name]
        checks = {
            "F1_beats_v0": bool(m["annual_return"] > v0["annual_return"]
                                and m["sharpe"] > v0["sharpe"]),
            "F2_mdd_ok": bool(m["mdd"] >= v0["mdd"] - 0.03),
            "F3_positive_without_best_5": bool(m["net_pnl_after_removing_best_5"] > 0),
            "F4_not_concentrated": bool(m["max_year_profit_share"] <= 0.40),
        }
        verdicts[name] = {**checks, "passes_all": all(checks.values())}

    payload = {
        "experiment": "revenue_inflection_v1", "run_date": str(date.today()),
        "registered": "research/EXPERIMENTS.md P3-12（2026-08-08，跑之前登記）",
        "environment": environment_block(data, parquet_dir=str(PARQUET_DIR)),
        "spec": {"inflection": "YoY(M) - YoY(M-1)",
                 "why_not_raw_mom": "台股月營收季節性強，原始月增率會把每年12月都標成剛開始變好",
                 "point_in_time": "沿用 _available_rev_month()，M月營收於M+1月10日後才可見",
                 "free_parameters": 0, "universe_markets": list(UNIVERSE_MARKETS)},
        "prior_expectation": "不看好（前四次症狀為真診斷為假；原始動機『避免追高』已被否決）",
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
