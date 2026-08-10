"""P3-10: does the score need a "how much can this move" dimension?

Pre-registered in research/EXPERIMENTS.md on 2026-08-08 before any run.

The quality pool is untouched -- only the choice of which 5 to buy from it changes:

    V0_baseline       quality top-20 -> top 5 by quality score  (current behaviour)
    V1_adr_floor      quality top-20 -> drop ADR20 < 4% -> top 5 by quality score
    V2_prefer_movers  quality top-20 -> re-sort by ADR20 desc  -> top 5

ADR20 and the 4% floor are taken from research/qullamaggie.py's QullamaggieSpec,
existing project constants rather than values tuned for this experiment.  V2 has
no free parameter at all.

development only, TWSE-only universe.  validation and holdout are not read.
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

from agent.backtest import _load, buy_and_hold_nav, perf_metrics
from agent.strategy import FEE_RATE, SLIPPAGE, STRATEGY, TAX_RATE, split_adjust
from research.data_splits import slice_dates
from research.provenance import environment_block
from research.qullamaggie import QullamaggieSpec, attach_unconditional_entry_paths
from scripts.run_qullamaggie_factorial import (
    PARQUET_DIR,
    current_candidates,
    prepare_research_data,
    run_factorial_backtest,
)

OUT_JSON = ROOT / "research" / "results" / "upside_dimension_2026-08-08.json"
OUT_MD = ROOT / "research" / "results" / "upside_dimension_2026-08-08.md"
PURPOSE = "P3-10 漲升空間維度：ADR 下限／偏好會動的標的"

UNIVERSE_MARKETS = ("TWSE",)
TOP_N = 5
POOL_N = 20
REBALANCE = 5
HORIZONS = (3, 5, 10, 20)

#: 沿用 QullamaggieSpec 的既有常數，非為本實驗新調
_QSPEC = QullamaggieSpec()
ADR_DAYS = _QSPEC.adr_days      # 20
ADR_MIN = _QSPEC.adr_min        # 0.04

#: F0：上市宇宙 dev 基準（P3-9b 已建立）
BASELINE = {"annual_return": 18.36, "sharpe": 1.13, "mdd": -15.15, "trades": 225}

VARIANTS = ("V0_baseline", "V1_adr_floor", "V2_prefer_movers")


def build_adr(panels: dict) -> pd.DataFrame:
    """ADR20，與 research/qullamaggie.py 同一算法（前一日收盤為分母）。"""
    highs, lows, closes = panels["highs"], panels["lows"], panels["closes"]
    prev_close = closes.shift(1)
    return ((highs - lows) / prev_close.where(prev_close > 0)).rolling(
        ADR_DAYS, min_periods=ADR_DAYS).mean()


def _adr(adr: pd.DataFrame, d, sid) -> float | None:
    try:
        v = adr.at[d, sid]
    except KeyError:
        return None
    return None if pd.isna(v) else float(v)


def build_schedules(data: dict, panels: dict, adr: pd.DataFrame,
                    dates: list, cfg: dict) -> tuple[dict, dict]:
    """三列共用同一個品質池，只改「挑哪 5 檔」。回傳 (schedules, 介入統計)。"""
    schedules = {v: {} for v in VARIANTS}
    stats = {"pool_days": 0, "pool_names": 0, "dropped_by_floor": 0,
             "no_adr": 0, "overlap_v2_v0": 0, "v2_days": 0,
             "dropped_examples": Counter()}

    for i, d in enumerate(dates):
        if i % REBALANCE != 0:
            continue
        pool = current_candidates(data, d, cfg, top_n=TOP_N)[:POOL_N]
        if not pool:
            continue
        stats["pool_days"] += 1
        stats["pool_names"] += len(pool)

        # 整池都要交給引擎：前幾名可能已持有或被族群上限擋下，引擎需要能往下遞補。
        # 先截成 5 檔會讓 V0 偏離已知基準——2026-08-08 首次執行就是這樣被 F0 抓到。
        if pool:
            schedules["V0_baseline"][d] = pool
        v0_head = pool[:TOP_N]

        kept = []
        for sid in pool:
            a = _adr(adr, d, sid)
            if a is None:
                stats["no_adr"] += 1
                continue
            if a < ADR_MIN:
                stats["dropped_by_floor"] += 1
                stats["dropped_examples"][sid] += 1
                continue
            kept.append(sid)
        if kept:
            schedules["V1_adr_floor"][d] = kept          # 同樣傳整池（已剔除者除外）

        scored = [(sid, _adr(adr, d, sid)) for sid in pool]
        scored = [(s, a) for s, a in scored if a is not None]
        movers = [s for s, _ in sorted(scored, key=lambda x: x[1], reverse=True)]
        if movers:
            schedules["V2_prefer_movers"][d] = movers    # 整池重排，不截斷
            stats["v2_days"] += 1
            stats["overlap_v2_v0"] += len(set(movers[:TOP_N]) & set(v0_head))

        if i % 250 == 0:
            print(f"  pool {i}/{len(dates)} {d}", flush=True)
    return schedules, stats


def _metrics(trades: pd.DataFrame, adr: pd.DataFrame, cfg: dict) -> dict:
    attrs = trades.attrs
    pnl = pd.to_numeric(trades["net_pnl"], errors="coerce").fillna(0.0)
    ordered = pnl.sort_values(ascending=False)
    gross = float(pnl[pnl > 0].sum())

    by_year, by_industry = defaultdict(float), defaultdict(float)
    for row in trades.itertuples(index=False):
        by_year[str(row.entry_date.year)] += float(row.net_pnl)

    # 低 ADR 標的的部位佔用——直接回應「它們是不是只是佔著額度」
    low_days = low_pnl = low_n = 0
    tot_days = 0
    for row in trades.itertuples(index=False):
        held = int(row.hold) + 1
        tot_days += held
        a = _adr(adr, row.entry_date, str(row.stock_id))
        if a is not None and a < ADR_MIN:
            low_days += held
            low_pnl += float(row.net_pnl)
            low_n += 1

    out = {
        "annual_return": float(attrs["ann_ret"]), "sharpe": float(attrs["sharpe"]),
        "mdd": float(attrs["nav_mdd"]), "calmar": float(attrs["calmar"]),
        "total_return": float(attrs["nav_total_ret"]), "trades": int(len(trades)),
        "win_rate": float((pnl > 0).mean()),
        "avg_hold_days": float(pd.to_numeric(trades["hold"], errors="coerce").mean()),
        "net_pnl": float(pnl.sum()),
        "net_pnl_after_removing_best_5": float(ordered.iloc[5:].sum()),
        "max_year_profit_share": (
            max((v / gross for v in by_year.values() if v > 0), default=0.0)
            if gross > 0 else 0.0),
        "low_adr_trades": low_n,
        "low_adr_position_day_share": low_days / max(1, tot_days),
        "low_adr_net_pnl": low_pnl,
        "bench_0050_return": float(attrs["bench_0050"]),
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
    adr = build_adr(panels)

    all_dates = sorted(pd.to_datetime(data["prices"]["trade_date"]).dt.date.unique())
    sel = slice_dates(all_dates, "development", purpose=PURPOSE)
    lo, hi = min(sel), max(sel)
    dates = [d for d in sorted(panels["closes"].index) if lo <= d <= hi]
    print(f"development {lo} ~ {hi}（{len(dates)} 交易日，宇宙={UNIVERSE_MARKETS}）", flush=True)

    schedules, stats = build_schedules(data, panels, adr, dates, cfg)
    pool_names = max(1, stats["pool_names"])
    treat = {
        "pool_days": stats["pool_days"],
        "floor_drop_rate": stats["dropped_by_floor"] / pool_names,
        "no_adr_rate": stats["no_adr"] / pool_names,
        "v2_overlap_with_v0": (stats["overlap_v2_v0"] / max(1, stats["v2_days"] * TOP_N)),
        "most_dropped": stats["dropped_examples"].most_common(20),
    }
    print(f"介入統計：剔除率 {treat['floor_drop_rate']:.1%}、"
          f"V2 與 V0 重疊 {treat['v2_overlap_with_v0']:.1%}", flush=True)

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
        results[name] = _metrics(trades, adr, cfg)
        m = results[name]
        print(f"{name:20s} ann={m['annual_return']:+.2%} S={m['sharpe']:.2f} "
              f"MDD={m['mdd']:+.2%} n={m['trades']} "
              f"低ADR佔倉={m['low_adr_position_day_share']:.1%}", flush=True)

    v0 = results["V0_baseline"]
    f0 = {
        "expected": BASELINE,
        "observed": {"annual_return": round(v0["annual_return"] * 100, 2),
                     "sharpe": round(v0["sharpe"], 2),
                     "mdd": round(v0["mdd"] * 100, 2), "trades": v0["trades"]},
    }
    f0["passed"] = (f0["observed"]["annual_return"] == BASELINE["annual_return"]
                    and f0["observed"]["sharpe"] == BASELINE["sharpe"]
                    and f0["observed"]["mdd"] == BASELINE["mdd"]
                    and f0["observed"]["trades"] == BASELINE["trades"])
    f0b = {
        "floor_drop_rate": treat["floor_drop_rate"],
        "v2_overlap_with_v0": treat["v2_overlap_with_v0"],
        "v1_applied": treat["floor_drop_rate"] >= 0.05,
        "v2_applied": treat["v2_overlap_with_v0"] <= 0.80,
    }
    f0b["passed"] = f0b["v1_applied"] and f0b["v2_applied"]

    verdicts = {}
    for name in ("V1_adr_floor", "V2_prefer_movers"):
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
        "experiment": "upside_dimension_v1", "run_date": str(date.today()),
        "registered": "research/EXPERIMENTS.md P3-10（2026-08-08，跑之前登記）",
        "environment": environment_block(data, parquet_dir=str(PARQUET_DIR)),
        "spec": {"adr_days": ADR_DAYS, "adr_min": ADR_MIN,
                 "source": "research/qullamaggie.py QullamaggieSpec（既有常數，非本實驗調整）",
                 "universe_markets": list(UNIVERSE_MARKETS)},
        "development": {"start": str(lo), "end": str(hi), "variants": results},
        "treatment_check": treat,
        "assessment": {"F0_baseline": f0, "F0b_treatment_applied": f0b,
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
