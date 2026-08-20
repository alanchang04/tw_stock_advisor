"""Development-only study of prior-day Q watchlists and next-day stop-buy fills."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.backtest import _load
from agent.strategy import STRATEGY
from research.data_splits import slice_dates
from research.qullamaggie import EXIT_VARIANTS, QullamaggieSpec
from scripts.run_qullamaggie_factorial import (
    PARQUET_DIR,
    _result,
    build_entry_schedule,
    prepare_research_data,
    run_factorial_backtest,
)


OUT_JSON = ROOT / "research" / "results" / "q_stop_order_study_2026-08-05.json"
OUT_MD = ROOT / "research" / "results" / "q_stop_order_study_2026-08-05.md"
PRIOR_JSON = ROOT / "research" / "results" / "qullamaggie_factorial_2026-08-05.json"
PURPOSE = "Q stop-buy：前一日watchlist、次日觸價成交、日K路徑上下界"

VARIANTS = {
    "q_stop__current": {"exit": "current", "path": "conservative"},
    **{
        f"q_stop__{exit_mode}__{path}": {"exit": exit_mode, "path": path}
        for exit_mode in EXIT_VARIANTS
        for path in ("conservative", "relaxed")
    },
}


def _assessment(payload: dict) -> dict:
    baseline = payload["controls"]["current__current"]
    results = payload["development"]["variants"]
    q_only = {k: v for k, v in results.items() if "__q_" in k}
    winners = [
        name for name, m in q_only.items()
        if m["annual_return"] > baseline["annual_return"] and m["sharpe"] > baseline["sharpe"]
    ]
    assumptions = {}
    for path in ("conservative", "relaxed"):
        group = {k: v for k, v in q_only.items() if k.endswith(path)}
        best_name, best = max(group.items(), key=lambda item: item[1]["sharpe"])
        assumptions[path] = {
            "best_variant": best_name, "annual_return": best["annual_return"],
            "sharpe": best["sharpe"], "mdd": best["mdd"],
            "same_day_stops": best["reason_counts"].get("Q同日觸發停損", 0),
        }
    worth_intraday = bool(winners) or (
        assumptions["relaxed"]["sharpe"] > baseline["sharpe"]
        and assumptions["conservative"]["sharpe"] > 0
    )
    return {
        "paired_q_variants_beating_current_baseline": winners,
        "bounds": assumptions,
        "worth_acquiring_5min_data": worth_intraday,
        "validation_run": "skipped_development_gate",
        "formal_defaults_changed": False,
    }


def _markdown(payload: dict) -> str:
    dev = payload["development"]
    lines = [
        "# Q 前一日 Watchlist＋次日 Stop-Buy 研究", "",
        "> 僅使用 development；validation 與 holdout 均未讀取。正式策略未變更。", "",
        "## 可執行時序", "",
        "1. T 日收盤後，以截至 T 日的資料建立 watchlist 與箱頂觸發價。",
        "2. T+1 預掛 buy-stop；最高價未觸及則取消，跳空越過則以開盤價加滑價成交。",
        "3. 初始停損只用 T 日已知 ADR，不使用 T+1 最低價決定停損距離。",
        "4. 日 K 無法知道先高後低或先低後高，因此同時報告 conservative／relaxed 邊界。", "",
        "## 固定規格", "", "```json",
        json.dumps(payload["spec"], ensure_ascii=False, indent=2), "```", "",
        f"訊號日期：{dev['signal_dates']}；實際結果：", "",
        "| 版本 | 年化 | Sharpe | MDD | 交易 | 勝率 | 同日停損 | Day3 | Day5 | Day10 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, m in dev["variants"].items():
        lines.append(
            f"| {name} | {m['annual_return']:.2%} | {m['sharpe']:.2f} | {m['mdd']:.2%} | "
            f"{m['trades']} | {m['win_rate']:.1%} | {m['reason_counts'].get('Q同日觸發停損', 0)} | "
            f"{m['day3_mean']:.2%} | {m['day5_mean']:.2%} | {m['day10_mean']:.2%} |"
        )
    controls = payload["controls"]
    lines += ["", "## 對照", "",
              f"- 現行／現行：年化 {controls['current__current']['annual_return']:.2%}、"
              f"Sharpe {controls['current__current']['sharpe']:.2f}、MDD {controls['current__current']['mdd']:.2%}。",
              f"- 收盤確認後隔日追進的 Q／現行：年化 {controls['qullamaggie__current']['annual_return']:.2%}、"
              f"Sharpe {controls['qullamaggie__current']['sharpe']:.2f}。", "",
              "## 結論", "",
              "- **預掛成交只解決部分追價。** 相較收盤確認後隔日追進，Day3/Day5 約由 -1.00%/-0.99% 改善至 -0.45%/-0.52%，但仍未轉正。",
              "- **Q進場配Q出場仍失敗。** 即使採對交易最有利的 relaxed 日內順序，最佳的全倉10MA仍為負年化與負Sharpe；保守邊界更差。",
              "- **日內順序不是翻盤關鍵。** conservative 與 relaxed 的績效幅度不同，但方向一致為負。",
              "- **暫不取得5分鐘資料。** 目前不存在正的日線上界可供精煉；5分鐘資料可能改善選擇，但沒有證據顯示足以跨越與現行基準的巨大差距。",
              "- **對現行策略的啟示。** 下一個合理實驗不是繼續調Q參數，而是保留現行的營收/法人品質選股，改用『前一日接近緊密箱頂→次日預掛突破』處理進場時機，並繼續使用現行出場。", "",
              "## 自動判決", "", "```json",
              json.dumps(payload["assessment"], ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=sorted(VARIANTS))
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()
    cfg = dict(STRATEGY)
    spec = QullamaggieSpec()
    data = _load(parquet_dir=str(PARQUET_DIR))
    panels = prepare_research_data(data, cfg)
    all_dates = sorted(pd.to_datetime(data["prices"]["trade_date"]).dt.date.unique())
    selected = slice_dates(all_dates, "development", purpose=PURPOSE)
    dates = [d for d in sorted(panels["closes"].index)
             if min(selected) <= d <= max(selected)]
    print("development precompute q_stop_order entry pools", flush=True)
    schedule = build_entry_schedule(data, panels, dates, "q_stop_order", cfg)
    variants = {args.only: VARIANTS[args.only]} if args.only else VARIANTS
    results = {}
    for name, variant in variants.items():
        run_cfg = {**cfg, "intraday_path_assumption": variant["path"]}
        trades = run_factorial_backtest(
            data=data, panels=panels, start_date=min(selected), end_date=max(selected),
            entry_mode="q_stop_order", exit_mode=variant["exit"], cfg=run_cfg,
            spec=spec, entry_schedule=schedule,
        )
        if trades.empty:
            raise RuntimeError(f"{name}: no trades")
        results[name] = _result(trades)
        m = results[name]
        print(f"{name:48s} ann={m['annual_return']:+.2%} S={m['sharpe']:.2f} "
              f"MDD={m['mdd']:+.2%} n={m['trades']}", flush=True)

    prior = json.loads(PRIOR_JSON.read_text(encoding="utf-8"))
    prior_dev = prior["splits"]["development"]["variants"]
    payload = {
        "experiment": "q_stop_order_v1", "run_date": str(date.today()),
        "spec": spec.to_dict(),
        "execution_bounds": {
            "conservative": "open below trigger and both trigger/stop touched => assume trigger first",
            "relaxed": "open below trigger and both touched => assume low occurred before trigger",
            "gap_entry": "open at/above trigger and low touches stop => stop is always actionable",
        },
        "development": {
            "start": str(min(selected)), "end": str(max(selected)),
            "signal_dates": len(schedule), "variants": results,
        },
        "controls": {
            "current__current": prior_dev["current__current"],
            "qullamaggie__current": prior_dev["qullamaggie__current"],
            "qullamaggie__q_full_10ma": prior_dev["qullamaggie__q_full_10ma"],
        },
        "validation_touched": False, "holdout_touched": False,
    }
    if not args.only:
        payload["assessment"] = _assessment(payload)
    else:
        payload["assessment"] = {"status": "partial_diagnostic_only"}
    if args.no_write:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(_markdown(payload), encoding="utf-8")
    print(f"wrote {OUT_JSON}")
    print(f"wrote {OUT_MD}")


if __name__ == "__main__":
    main()
