"""Pre-registered P3-6 swing risk experiments (development + one validation batch).

Formal STRATEGY defaults are never mutated. The untouched holdout is deliberately not read.
"""
from __future__ import annotations

import json
import sys
import argparse
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.backtest import _load, run_backtest
from agent.strategy import STRATEGY
from research.data_splits import slice_dates


PARQUET_DIR = ROOT / "data" / "research"
OUT_JSON = ROOT / "research" / "results" / "swing_risk_experiments_2026-08-05.json"
OUT_MD = ROOT / "research" / "results" / "swing_risk_experiments_2026-08-05.md"

VARIANTS = {
    "baseline": {},
    "stop_open_revalidation": {"revalidate_stop_at_open": True},
    "atr_risk_equal": {
        "stop_mode": "atr", "atr_period": 14, "atr_stop_multiple": 2.5,
        "atr_stop_min_pct": 0.05, "atr_stop_max_pct": 0.12,
    },
    "reentry_state_machine": {
        "reentry_enabled": True, "reentry_cooldown_days": 2,
        "reentry_watch_days": 20, "reentry_rank_pool": 20,
        "reentry_position_scale": 0.50,
        "reentry_require_above_ma20": True,
        "reentry_require_above_exit": True,
    },
    "tiered_market_exposure": {
        "market_exposure_mode": "tiered", "neutral_exposure_scale": 0.60,
        "risk_off_exposure_scale": 0.30, "risk_off_breadth_threshold": 0.40,
    },
}


def _metrics(trades: pd.DataFrame) -> dict:
    attrs = trades.attrs
    net_pnl = pd.to_numeric(trades.get("net_pnl"), errors="coerce").dropna()
    ordered = net_pnl.sort_values(ascending=False)
    robustness = {}
    for n in (1, 5, 10):
        robustness[f"net_pnl_after_removing_best_{n}"] = float(ordered.iloc[n:].sum())
    nav = pd.Series(attrs.get("nav") or {}, dtype=float).sort_index()
    yearly = {}
    if len(nav):
        for year, series in nav.groupby(pd.to_datetime(nav.index).year):
            yearly[str(year)] = float(series.iloc[-1] / series.iloc[0] - 1)
    return {
        "trades": int(len(trades)),
        "win_rate": float(attrs.get("net_win")),
        "total_return": float(attrs.get("nav_total_ret")),
        "annual_return": float(attrs.get("ann_ret")),
        "sharpe": float(attrs.get("sharpe")),
        "mdd": float(attrs.get("nav_mdd")),
        "calmar": float(attrs.get("calmar")),
        "bench_0050_return": float(attrs.get("bench_0050")),
        "bench_0050_sharpe": float(attrs.get("sharpe_0050")),
        "bench_0050_mdd": float(attrs.get("mdd_0050")),
        "reason_counts": {str(k): int(v) for k, v in trades["reason"].value_counts().items()},
        "entry_kind_counts": {
            str(k): int(v) for k, v in trades["entry_kind"].value_counts().items()
        },
        "yearly_returns": yearly,
        **robustness,
    }


def _run_split(data: dict, split: str, variants: dict | None = None) -> dict:
    dates = sorted(pd.to_datetime(data["prices"]["trade_date"]).dt.date.unique())
    selected = slice_dates(
        dates, split,
        purpose="P3-6 預先登記五組波段風控（一次批次；不挑參數）",
    )
    if not selected:
        raise RuntimeError(f"{split} has no data")
    results = {}
    for name, overrides in (variants or VARIANTS).items():
        cfg = {**STRATEGY, **overrides}
        trades = run_backtest(
            cfg=cfg, data=data, quiet=True,
            start_date=min(selected), end_date=max(selected),
        )
        if trades is None or trades.empty:
            raise RuntimeError(f"{split}/{name} produced no trades")
        results[name] = _metrics(trades)
        print(f"{split:11s} {name:25s} "
              f"ann={results[name]['annual_return']:+.2%} "
              f"sharpe={results[name]['sharpe']:.2f} "
              f"mdd={results[name]['mdd']:+.2%}")
    return {"start": str(min(selected)), "end": str(max(selected)), "variants": results}


def _markdown(payload: dict) -> str:
    lines = [
        "# P3-6 波段風控預先登記實驗", "",
        "> 僅使用 development 與一次性 validation 批次；未讀取 holdout。正式預設未改。", "",
    ]
    for split, section in payload["splits"].items():
        lines += [f"## {split}（{section['start']}～{section['end']}）", "",
                  "| 版本 | 年化 | Sharpe | MDD | Calmar | 交易數 | 0050總報酬* |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for name, m in section["variants"].items():
            lines.append(
                f"| {name} | {m['annual_return']:.2%} | {m['sharpe']:.2f} | "
                f"{m['mdd']:.2%} | {m['calmar']:.2f} | {m['trades']} | "
                f"{m['bench_0050_return']:.2%} |"
            )
        lines += ["", "\\* 最後一欄是同期間 0050 總報酬（含相同進出摩擦），不是年化。", ""]
    lines += [
        "## 判讀紀律", "",
        "- 先看 development 的方向，再看 validation 是否同向；validation 翻轉即否決。",
        "- 單一個股案例不構成通過；不因結果再掃 ATR 倍數、冷卻天數或曝險比例。",
        "- 本輪不碰 holdout，也不把最佳變體直接設成正式策略。",
        "", "## 結論", "",
        "- **隔日開盤收復停損線：否決。** development 報酬只小幅增加但 MDD 變差；validation 年化與 Sharpe 都略差於 baseline。",
        "- **重進場狀態機：此規格無樣本，否決部署但不否決概念。** development 觸發診斷為 0 次；差異來自觀察期暫停一般重買。",
        "- **三級市場曝險：否決部署。** development 最漂亮，但 validation Sharpe 反而更差，屬於 régime 翻轉。",
        "- **ATR 等風險：僅保留候選。** 兩段方向大致改善，但 validation 仍嚴重為負且弱於 0050；不啟用、不再掃參數。",
        "", "總判決：五組都沒有達到部署門檻，正式 STRATEGY 維持原設定。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=sorted(VARIANTS),
                        help="diagnostic rerun of one predeclared variant")
    parser.add_argument("--split", choices=("development", "validation"), action="append",
                        help="limit diagnostic rerun; default runs both")
    parser.add_argument("--no-write", action="store_true",
                        help="print results without replacing the registered report")
    args = parser.parse_args()
    data = _load(parquet_dir=str(PARQUET_DIR))
    selected_variants = ({args.only: VARIANTS[args.only]} if args.only else VARIANTS)
    selected_splits = args.split or ["development", "validation"]
    payload = {
        "experiment": "P3-6", "run_date": str(date.today()),
        "variants": selected_variants,
        "assessment": {
            "deployment": "rejected_all",
            "research_candidate_only": "atr_risk_equal",
            "formal_defaults_changed": False,
        },
        "splits": {}, "holdout_touched": False,
    }
    for split in selected_splits:
        payload["splits"][split] = _run_split(data, split, selected_variants)
    reentry_dev = (payload["splits"].get("development", {}).get("variants", {})
                   .get("reentry_state_machine"))
    if reentry_dev:
        n_reentry = int(reentry_dev.get("entry_kind_counts", {}).get("reentry", 0))
        payload["assessment"]["reentry_development_trigger_count"] = n_reentry
        if n_reentry == 0:
            payload["assessment"]["reentry_interpretation"] = (
                "no_sample_do_not_reject_the_broader_concept"
            )
    if args.no_write:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(_markdown(payload), encoding="utf-8")
    print(f"wrote {OUT_JSON}")
    print(f"wrote {OUT_MD}")


if __name__ == "__main__":
    main()
