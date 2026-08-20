"""P3-9: does the production quality ranking buy at the wrong chart stage?

Pre-registered in ``research/EXPERIMENTS.md`` on 2026-08-06 *before* any run,
including every threshold and all six failure conditions F0-F5.

Only entry timing varies.  The 8% stop, the production exit rules, the market
filter, the sector cap, the universe filters and the cost model are held at their
production values across all five columns -- otherwise an improvement could not be
attributed to timing.

development only.  validation is not read (August's single access was spent by
P3-6 on 2026-08-05) and holdout is not read.
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
from research.qullamaggie import attach_unconditional_entry_paths
from research.quality_timing import (
    QualityTimingSpec,
    build_quality_box_panels,
    select_quality_stop_buy_orders,
)
from scripts.run_qullamaggie_factorial import (
    PARQUET_DIR,
    current_candidates,
    prepare_research_data,
    run_factorial_backtest,
)

OUT_JSON = ROOT / "research" / "results" / "quality_breakout_timing_2026-08-06.json"
OUT_MD = ROOT / "research" / "results" / "quality_breakout_timing_2026-08-06.md"
PURPOSE = "P3-9 品質選股×可執行突破時機：C0/C1 對照 + H1/H2/H3 箱型預掛"

SPEC = QualityTimingSpec()
HORIZONS = (3, 5, 10, 20)
REBALANCE = 5

#: P3-9b: research runs on a TWSE-only universe.  TPEX institutional data starts
#: 2018-01, so a 2015-2020 study that includes TPEX names switches universe
#: halfway through and no cross-year comparison holds.  Adopted for consistency,
#: not for returns (same footing as exclude_disposition in #43).
#: Live keeps STRATEGY["universe_markets"] = None; its data is complete.
UNIVERSE_MARKETS = ("TWSE",)

#: F0 reference: the unmodified factorial's ``current__current`` cell, recomputed
#: on 2026-08-06 after the two machines' data was fully reconciled.
#:
#: The published 13.55%/0.81/-26.64%/236 is now exactly reproducible here, but only
#: by *disabling* the disposition gate -- the other machine simply had no
#: disposition_events.parquet, so `exclude_disposition` was silently inactive for
#: all of P3-6/P3-7/P3-8.  #43 adopted that gate deliberately ("理由是寫實度不是
#: 報酬"), so the correct configuration is the gate ON, which gives the figures
#: below.  13.55% is reproducible; 13.87% is right.
#:
#: Reconciliation chain (all four figures matched at each step):
#:   missing universe metadata      -> 14.07% / 242 trades
#:   + stocks/delisted metadata     -> 14.43% / 236
#:   + synced price & inst data     -> 13.87% / 236   <- this baseline
#:   + disposition gate off         -> 13.55% / 236   <- other machine
BASELINE = {"annual_return": 13.87, "sharpe": 0.83, "mdd": -25.59, "trades": 236}

#: Pre-registered.  Do not tune against results; do not scan neighbours (P3-5).
VARIANTS = {
    "C0_formal_5d": {"entry": "current", "cadence": REBALANCE},
    "C1_daily_next_open": {"entry": "current", "cadence": 1},
    "H1_quality_stop_buy": {
        "entry": "quality_stop_buy", "cadence": 1,
        "require_tight": False, "require_dryup": False,
    },
    "H2_quality_tight_stop_buy": {
        "entry": "quality_stop_buy", "cadence": 1,
        "require_tight": True, "require_dryup": False,
    },
    "H3_quality_tight_dryup": {
        "entry": "quality_stop_buy", "cadence": 1,
        "require_tight": True, "require_dryup": True,
    },
}
LADDER = ("H1_quality_stop_buy", "H2_quality_tight_stop_buy", "H3_quality_tight_dryup")

#: F4 operationalisation, fixed before running: no single calendar year and no
#: single industry may supply more than this share of gross profit.
CONCENTRATION_MAX = 0.40
#: F2: a candidate may not deepen drawdown by more than this versus C0.
MDD_TOLERANCE = 0.03


def build_daily_quality_pool(data: dict, dates: list, cfg: dict) -> dict:
    """Production quality top-20 for every date, computed once and shared.

    All five columns rank stocks identically; only the timing layer differs.  The
    five-day column is exactly the ``i % 5 == 0`` subset of this pool, which keeps
    C0 an exact reproduction of the published baseline rather than a near-miss.
    """
    pool = {}
    for i, d in enumerate(dates):
        candidates = current_candidates(data, d, cfg, top_n=5)
        if candidates:
            pool[d] = candidates
        if i % 250 == 0:
            print(f"  quality pool {i}/{len(dates)} {d}", flush=True)
    return pool


def build_schedule(variant: dict, pool: dict, dates: list, boxes: dict) -> dict:
    """Turn the shared quality pool into one variant's entry schedule."""
    if variant["entry"] == "current":
        cadence = variant["cadence"]
        return {
            d: pool[d]
            for i, d in enumerate(dates)
            if i % cadence == 0 and d in pool
        }
    schedule = {}
    for d in dates:
        ranked = pool.get(d)
        if not ranked:
            continue
        orders = select_quality_stop_buy_orders(
            ranked, boxes, d, SPEC,
            require_tight=variant["require_tight"],
            require_dryup=variant["require_dryup"],
        )
        if orders:
            schedule[d] = orders
    return schedule


def _yearly_returns(nav: pd.Series, initial: float) -> dict:
    series = nav.copy()
    series.index = pd.to_datetime(list(series.index))
    out, prev = {}, float(initial)
    for year, group in series.groupby(series.index.year):
        end = float(group.iloc[-1])
        out[str(year)] = end / prev - 1.0 if prev else 0.0
        prev = end
    return out


def _profit_shares(values: dict) -> dict:
    """Share of gross profit by bucket.  Losses are excluded from the base."""
    gross = sum(v for v in values.values() if v > 0)
    if gross <= 0:
        return {}
    return {k: v / gross for k, v in sorted(
        values.items(), key=lambda kv: kv[1], reverse=True
    )}


def _metrics(trades: pd.DataFrame, order_log: list, data: dict,
             n_days: int, cfg: dict) -> dict:
    attrs = trades.attrs
    nav = pd.Series(attrs["nav"], dtype=float).sort_index()
    initial = float(cfg["capital"])
    pnl = pd.to_numeric(trades["net_pnl"], errors="coerce").fillna(0.0)
    ordered = pnl.sort_values(ascending=False)
    gross_profit = float(pnl[pnl > 0].sum())
    max_open = int(cfg.get("max_open_positions", 10))

    by_year, by_industry = defaultdict(float), defaultdict(float)
    mapping = data.get("_sid_to_inds") or {}
    for row in trades.itertuples(index=False):
        by_year[str(row.entry_date.year)] += float(row.net_pnl)
        industries = sorted(mapping.get(str(row.stock_id), ()))
        by_industry[industries[0] if industries else "未分類"] += float(row.net_pnl)

    outcomes = Counter(r["outcome"] for r in order_log)
    live = outcomes["triggered"] + outcomes["gap_open"] + outcomes["cancelled_not_triggered"]
    filled_stop = outcomes["triggered"] + outcomes["gap_open"]

    result = {
        "total_return": float(attrs["nav_total_ret"]),
        "annual_return": float(attrs["ann_ret"]),
        "sharpe": float(attrs["sharpe"]),
        "mdd": float(attrs["nav_mdd"]),
        "calmar": float(attrs["calmar"]),
        "trades": int(len(trades)),
        "win_rate": float((pnl > 0).mean()),
        "avg_hold_days": float(pd.to_numeric(trades["hold"], errors="coerce").mean()),
        "mfe_mean": float(pd.to_numeric(trades["mfe"], errors="coerce").mean()),
        "mae_mean": float(pd.to_numeric(trades["mae"], errors="coerce").mean()),
        # Slot exposure: how much of the available position capacity was used.
        "slot_exposure": float(
            sum(int(h) + 1 for h in trades["hold"]) / max(1, max_open * n_days)
        ),
        "turnover_per_year": float(
            pd.to_numeric(trades["buy_cost"], errors="coerce").sum()
            / max(1.0, float(nav.mean())) / max(1e-9, n_days / 252.0)
        ),
        "stop_rate": float(
            trades["reason"].astype(str).str.contains("停損").mean()
        ),
        "reason_counts": {str(k): int(v) for k, v in trades["reason"].value_counts().items()},
        "top1_profit_share": (
            float(ordered.head(1).clip(lower=0).sum() / gross_profit) if gross_profit > 0 else None
        ),
        "top5_profit_share": (
            float(ordered.head(5).clip(lower=0).sum() / gross_profit) if gross_profit > 0 else None
        ),
        "top10_profit_share": (
            float(ordered.head(10).clip(lower=0).sum() / gross_profit) if gross_profit > 0 else None
        ),
        "net_pnl": float(pnl.sum()),
        "net_pnl_after_removing_best_5": float(ordered.iloc[5:].sum()),
        "total_return_after_removing_best_5": float(ordered.iloc[5:].sum() / initial),
        "yearly_returns": _yearly_returns(nav, initial),
        "profit_share_by_year": _profit_shares(dict(by_year)),
        "profit_share_by_industry": _profit_shares(dict(by_industry)),
        "order_outcomes": {k: int(v) for k, v in outcomes.items()},
        # Denominators are explicit: a cancel rate over *live* orders is not the
        # same as one diluted by orders never placed for lack of a free slot.
        "cancel_rate_of_live_orders": (
            float(outcomes["cancelled_not_triggered"] / live) if live else None
        ),
        "gap_fill_rate_of_stop_fills": (
            float(outcomes["gap_open"] / filled_stop) if filled_stop else None
        ),
        "bench_0050_return": float(attrs["bench_0050"]),
        "bench_0050_sharpe": float(attrs["sharpe_0050"]),
        "bench_0050_mdd": float(attrs["mdd_0050"]),
    }
    for horizon in HORIZONS:
        column = f"ret_day{horizon}"
        values = pd.to_numeric(trades.get(column), errors="coerce").dropna()
        result[f"day{horizon}_n"] = int(len(values))
        result[f"day{horizon}_mean"] = float(values.mean()) if len(values) else None
        result[f"day{horizon}_positive_rate"] = (
            float((values > 0).mean()) if len(values) else None
        )
    return result


def _baseline_check(m: dict) -> dict:
    """F0 part 1: the unrestricted path must be untouched by the P3-9b change.

    Run with ``universe_markets=None`` this must reproduce the pre-change figures
    exactly.  That is the real engine-integrity test -- it asks whether adding the
    venue restriction perturbed anything it should not have.  Published figures
    are rounded, so compare at published precision.
    """
    checks = {
        "annual_return": round(m["annual_return"] * 100, 2) == BASELINE["annual_return"],
        "sharpe": round(m["sharpe"], 2) == BASELINE["sharpe"],
        "mdd": round(m["mdd"] * 100, 2) == BASELINE["mdd"],
        "trades": m["trades"] == BASELINE["trades"],
    }
    return {
        "purpose": "universe_markets=None 時必須與改動前完全相同（no-op 回歸檢查）",
        "expected": BASELINE,
        "observed": {
            "annual_return": round(m["annual_return"] * 100, 2),
            "sharpe": round(m["sharpe"], 2),
            "mdd": round(m["mdd"] * 100, 2),
            "trades": m["trades"],
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def _assessment(results: dict) -> dict:
    c0 = results["C0_formal_5d"]
    verdicts = {}
    for name in LADDER:
        m = results[name]
        year_max = max(m["profit_share_by_year"].values(), default=0.0)
        industry_max = max(m["profit_share_by_industry"].values(), default=0.0)
        checks = {
            "F1_beats_c0_on_return_and_sharpe": bool(
                m["annual_return"] > c0["annual_return"] and m["sharpe"] > c0["sharpe"]
            ),
            "F2_mdd_not_worse_by_3pp": bool(m["mdd"] >= c0["mdd"] - MDD_TOLERANCE),
            "F3_positive_without_best_5": bool(m["net_pnl_after_removing_best_5"] > 0),
            "F4_not_concentrated": bool(
                year_max <= CONCENTRATION_MAX and industry_max <= CONCENTRATION_MAX
            ),
        }
        verdicts[name] = {
            **checks,
            "max_year_profit_share": year_max,
            "max_industry_profit_share": industry_max,
            "passes_all": all(checks.values()),
        }
    passing = [n for n in LADDER if verdicts[n]["passes_all"]]
    sharpes = [results[n]["sharpe"] for n in LADDER]
    monotone = sharpes == sorted(sharpes) or sharpes == sorted(sharpes, reverse=True)
    # F5: a win that appears only in the middle rung, with both neighbours failing,
    # is the single-peak pattern P3-5 was rejected for.
    single_peak = passing == [LADDER[1]]
    return {
        "baseline_reproduced": results["C0_formal_5d"]["_f0"],
        "cadence_effect_c1_minus_c0": {
            "annual_return": results["C1_daily_next_open"]["annual_return"] - c0["annual_return"],
            "sharpe": results["C1_daily_next_open"]["sharpe"] - c0["sharpe"],
            "mdd": results["C1_daily_next_open"]["mdd"] - c0["mdd"],
        },
        "per_variant": verdicts,
        "passing_variants": passing,
        "F5_ladder_sharpes": dict(zip(LADDER, sharpes)),
        "F5_monotone": bool(monotone),
        "F5_single_peak_warning": bool(single_peak),
        "thresholds_frozen": {
            "max_distance": SPEC.max_distance,
            "tightness_max": SPEC.tightness_max,
            "dryup_days_vs_box_days": [SPEC.dryup_days, SPEC.box_days],
            "note": "看到結果後不得調整，也不得掃鄰近值（§4.3 鄰居檢驗 / P3-5）",
        },
        "validation_touched": False,
        "holdout_touched": False,
        "formal_defaults_changed": False,
    }


def _markdown(payload: dict) -> str:
    dev = payload["development"]
    results = dev["variants"]
    assessment = payload["assessment"]
    lines = [
        "# P3-9 品質選股 × 可執行突破時機", "",
        "> development only（2015-01~2020-12）。validation 未讀取（2026-08 額度已由 P3-6 用掉）、",
        "> holdout 未讀取。正式 `STRATEGY` 未變更。所有門檻在看到結果前已寫進 `research/EXPERIMENTS.md`。", "",
        "## 這一輪只動進場", "",
        "停損固定 8%、出場用現行 `decide_exit`、market filter、sector cap、宇宙濾網與成本口徑",
        "全部維持正式值，五列共用同一個 portfolio engine 與同一份品質排名。", "",
        "## 執行環境（換環境數字就不同，比對前先核對）", "", "```json",
        json.dumps(payload["environment"], ensure_ascii=False, indent=2), "```", "",
        "## F0 基準重現", "", "```json",
        json.dumps(assessment["baseline_reproduced"], ensure_ascii=False, indent=2), "```", "",
        "## 主結果", "",
        "| 版本 | 年化 | Sharpe | MDD | Calmar | 交易 | 勝率 | 平均持有 | 曝險 | 週轉/年 | 停損率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, m in results.items():
        lines.append(
            f"| {name} | {m['annual_return']:.2%} | {m['sharpe']:.2f} | {m['mdd']:.2%} | "
            f"{m['calmar']:.2f} | {m['trades']} | {m['win_rate']:.1%} | {m['avg_hold_days']:.1f} | "
            f"{m['slot_exposure']:.1%} | {m['turnover_per_year']:.2f} | {m['stop_rate']:.1%} |"
        )
    lines += ["", "## 無條件進場路徑（提前停損者仍繼續追蹤原股票）", "",
              "| 版本 | Day3 | Day5 | Day10 | Day20 | Day20>0 佔比 | MFE | MAE |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, m in results.items():
        def _fmt(value):
            return "—" if value is None else f"{value:.2%}"
        lines.append(
            f"| {name} | {_fmt(m['day3_mean'])} | {_fmt(m['day5_mean'])} | "
            f"{_fmt(m['day10_mean'])} | {_fmt(m['day20_mean'])} | "
            f"{_fmt(m['day20_positive_rate'])} | {m['mfe_mean']:.2%} | {m['mae_mean']:.2%} |"
        )
    lines += ["", "## 成交品質與右尾集中度", "",
              "| 版本 | 未觸價取消率 | 跳空成交率 | Top1 | Top5 | Top10 | 移除最佳5筆後淨損益 |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for name, m in results.items():
        def _pct(value):
            return "—" if value is None else f"{value:.1%}"
        lines.append(
            f"| {name} | {_pct(m['cancel_rate_of_live_orders'])} | "
            f"{_pct(m['gap_fill_rate_of_stop_fills'])} | {_pct(m['top1_profit_share'])} | "
            f"{_pct(m['top5_profit_share'])} | {_pct(m['top10_profit_share'])} | "
            f"{m['net_pnl_after_removing_best_5']:,.0f} |"
        )
    years = sorted({y for m in results.values() for y in m["yearly_returns"]})
    lines += ["", "## 分年報酬（含 0050 同成本口徑）", "",
              "| 版本 | " + " | ".join(years) + " |",
              "|---" * (len(years) + 1) + "|"]
    for name, m in results.items():
        cells = [f"{m['yearly_returns'].get(y, float('nan')):.1%}" for y in years]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    bench = dev["benchmark_0050"]
    cells = [f"{bench['yearly_returns'].get(y, float('nan')):.1%}" for y in years]
    lines.append("| **0050 買進持有** | " + " | ".join(cells) + " |")
    lines += ["", f"0050 同期：總報酬 {bench['total_return']:.2%}、年化 {bench['annual_return']:.2%}、"
              f"Sharpe {bench['sharpe']:.2f}、MDD {bench['mdd']:.2%}。", "",
              "## 預先登記判決（F0~F5）", "", "```json",
              json.dumps(assessment, ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=sorted(VARIANTS))
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()

    # Research universe is TWSE-only (P3-9b).  `cfg_unrestricted` exists purely to
    # run the F0 no-op regression -- it must still reproduce the pre-change figures.
    cfg_unrestricted = dict(STRATEGY)
    cfg = {**STRATEGY, "universe_markets": UNIVERSE_MARKETS}
    data = _load(parquet_dir=str(PARQUET_DIR))
    panels = prepare_research_data(data, cfg)
    boxes = build_quality_box_panels(
        panels["highs"], panels["lows"], panels["closes"], panels["volume"], SPEC
    )
    all_dates = sorted(pd.to_datetime(data["prices"]["trade_date"]).dt.date.unique())
    selected = slice_dates(all_dates, "development", purpose=PURPOSE)
    lo, hi = min(selected), max(selected)
    dates = [d for d in sorted(panels["closes"].index) if lo <= d <= hi]
    print(f"development {lo} ~ {hi}（{len(dates)} 個交易日）", flush=True)

    # F0 part 1: unrestricted C0 must be bit-for-bit what it was before P3-9b.
    print("F0 regression: C0 with universe_markets=None", flush=True)
    pool_unrestricted = build_daily_quality_pool(data, dates, cfg_unrestricted)
    f0_schedule = build_schedule(VARIANTS["C0_formal_5d"], pool_unrestricted, dates, boxes)
    f0_trades = run_factorial_backtest(
        data=data, panels=panels, start_date=lo, end_date=hi,
        entry_mode="current", exit_mode="current", cfg=cfg_unrestricted,
        rebalance=REBALANCE, entry_schedule=f0_schedule,
    )
    f0_trades = attach_unconditional_entry_paths(f0_trades, panels["closes"],
                                                 horizons=HORIZONS)
    f0_metrics = _metrics(f0_trades, [], data, len(dates), cfg_unrestricted)
    f0 = _baseline_check(f0_metrics)
    print(f"  observed {f0['observed']} -> passed={f0['passed']}", flush=True)

    print(f"building shared quality pool (universe={UNIVERSE_MARKETS})", flush=True)
    pool = build_daily_quality_pool(data, dates, cfg)
    print(f"quality pool ready: {len(pool)} 個有候選的交易日", flush=True)

    variants = {args.only: VARIANTS[args.only]} if args.only else VARIANTS
    results = {}
    for name, variant in variants.items():
        schedule = build_schedule(variant, pool, dates, boxes)
        order_log: list = []
        trades = run_factorial_backtest(
            data=data, panels=panels, start_date=lo, end_date=hi,
            entry_mode=variant["entry"], exit_mode="current", cfg=cfg,
            rebalance=variant["cadence"], entry_schedule=schedule,
            order_log=order_log,
        )
        if trades.empty:
            raise RuntimeError(f"{name}: no trades")
        trades = attach_unconditional_entry_paths(
            trades, panels["closes"], horizons=HORIZONS
        )
        results[name] = _metrics(trades, order_log, data, len(dates), cfg)
        results[name]["signal_days"] = len(schedule)
        m = results[name]
        print(f"{name:32s} ann={m['annual_return']:+.2%} S={m['sharpe']:.2f} "
              f"MDD={m['mdd']:+.2%} n={m['trades']} 訊號日={len(schedule)}", flush=True)

    market_sid = cfg.get("market_filter_stock", "0050")
    if market_sid not in panels["closes"].columns:
        raise RuntimeError(f"benchmark {market_sid} missing; refusing to report without it")
    market_close = split_adjust(panels["closes"][market_sid]).reindex(dates).ffill()
    nav0050 = buy_and_hold_nav(
        market_close, cfg["capital"], fee_rate=FEE_RATE, tax_rate=TAX_RATE,
        slippage=SLIPPAGE,
    )
    bench_metrics = perf_metrics(nav0050)
    benchmark = {
        "total_return": float(bench_metrics["total"]),
        "annual_return": float(bench_metrics["ann_ret"]),
        "sharpe": float(bench_metrics["sharpe"]),
        "mdd": float(bench_metrics["mdd"]),
        "yearly_returns": _yearly_returns(nav0050, float(cfg["capital"])),
    }

    payload = {
        "experiment": "quality_breakout_timing_v1",
        "run_date": str(date.today()),
        "registered": "research/EXPERIMENTS.md P3-9b（2026-08-06，跑之前登記）",
        # Without this, a failed reproduction on another machine is indistinguishable
        # from a code regression.  See research/provenance.py.
        "environment": environment_block(data, parquet_dir=str(PARQUET_DIR)),
        "spec": SPEC.to_dict(),
        "universe_markets": list(UNIVERSE_MARKETS),
        "second_look_warning": (
            "本輪為對同一假說的第二次檢定（P3-9 已在含櫃買的宇宙上判過一次，"
            "五列全數否決）。若本輪出現通過者，依 P3-9b 登記不得逕行採信，"
            "須計入多重檢定並列為待下一輪獨立設計檢驗。"
        ),
        "variants_declared": VARIANTS,
        "development": {
            "start": str(lo), "end": str(hi), "trading_days": len(dates),
            "variants": results, "benchmark_0050": benchmark,
        },
        "validation_touched": False,
        "holdout_touched": False,
    }
    if not args.only:
        results["C0_formal_5d"]["_f0"] = f0
        payload["assessment"] = _assessment(results)
    else:
        payload["assessment"] = {"status": "partial_diagnostic_only"}

    if args.no_write:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    OUT_MD.write_text(_markdown(payload), encoding="utf-8")
    print(f"wrote {OUT_JSON}")
    print(f"wrote {OUT_MD}")


if __name__ == "__main__":
    main()
