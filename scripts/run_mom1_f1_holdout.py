"""MOM-1 F1：2008~2014 backward holdout **一次性否證測試**（SPEC §9.2）。

> **這個腳本執行一次，結果不論好壞都寫進登記簿。**
> 依 SPEC §9.2 的不對稱結論效力：
> 未通過 ⇒ MOM-1 在乾淨資料上被否證，依 §9.5 正式否決。
> 通過   ⇒ 只能宣稱「在 2005~2014 的市場制度下沒有被否證」，
>          **不得**宣稱「策略有效」「可以部署」「現制度下成立」。
> 通過 F1 只代表省下 12 個月的 forward paper，不構成任何正面證據。

執行前置條件（腳本會自行檢查，不通過就中止）：

1. release 必須是 `tw_stock_data_2005_2014_r3`，且其 `usage_policy` 允許
   backward holdout 績效檢視。在 r2 之下執行會違反該 release 自己的政策。
2. 策略契約檔案必須已 commit——報告要記錄 commit SHA，
   工作區有未提交的變更就無法宣稱「跑的是哪一版」。

MOM-1A 是控制組（不要求通過部署門檻），MOM-1B 是唯一部署候選，
**兩者結果都必須揭露**（§9.2）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.momentum import compute_mom_6_1  # noqa: E402
from research.momentum_backtest import benchmark_nav, simulate  # noqa: E402
from research.momentum_release import load_momentum_release, sha256_file  # noqa: E402
from research.performance import (  # noqa: E402
    block_bootstrap_monthly, core_metrics, f1_criteria, monthly_profile,
    relative_metrics, trade_concentration,
)

RELEASE_ID = "tw_stock_data_2005_2014_r3"
HOLDOUT_START = "2008-01-01"
HOLDOUT_END = "2014-12-31"
LEDGER_PATH = "reports/twse_execution_action_ledger_2005_2014.csv"
REQUIRED_POLICY = "backward holdout performance inspection"

STRATEGY_PATHS = (
    "research/momentum.py",
    "research/momentum_execution.py",
    "research/momentum_backtest.py",
    "research/momentum_corporate_actions.py",
    "research/momentum_release.py",
    "research/performance.py",
    "scripts/run_mom1_f1_holdout.py",
    "docs/SPEC_DATA_FOUNDATION_AND_MOMENTUM.md",
)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, check=True).stdout.strip()


def _assert_policy_allows(release_id: str) -> dict:
    directory = ROOT / "reports/data_releases"
    matches = [json.loads(p.read_text(encoding="utf-8"))
               for p in sorted(directory.glob("*.json"))
               if json.loads(p.read_text(encoding="utf-8")).get("data_release_id") == release_id]
    if len(matches) != 1:
        raise SystemExit(f"找不到唯一的 {release_id} descriptor")
    descriptor = matches[0]
    policy = descriptor.get("usage_policy", {})
    if REQUIRED_POLICY in policy.get("blocked", []):
        raise SystemExit(
            f"{release_id} 的 usage_policy 仍封鎖 {REQUIRED_POLICY!r}；"
            "先發佈解封的 release 再執行 F1")
    if not descriptor.get("readiness", {}).get("backward_holdout_performance_ready"):
        raise SystemExit(f"{release_id} 的 readiness 尚未標示 backward holdout 可檢視")
    return descriptor


def _assert_contract_committed() -> str:
    dirty = _git("status", "--porcelain", "--", *STRATEGY_PATHS)
    if dirty:
        raise SystemExit(
            "F1 是一次性測試，策略契約必須先 commit 才能執行：\n" + dirty)
    return _git("log", "-1", "--format=%H", "--", *STRATEGY_PATHS)


def summarise(result, benchmark: pd.Series, draws: int) -> dict:
    metrics = core_metrics(result.nav)
    relative = relative_metrics(result.nav, benchmark)
    profile = monthly_profile(result.nav)
    concentration = trade_concentration(result.trades)
    bootstrap = block_bootstrap_monthly(result.nav, draws=draws)
    criteria = f1_criteria(nav=result.nav, benchmark=benchmark, trades=result.trades)

    diagnostics = dict(result.diagnostics)
    costs = (diagnostics["total_commission_twd"]
             + diagnostics["total_transaction_tax_twd"])
    capital = diagnostics["capital_twd"]
    gross_profit = concentration.get("gross_profit", 0.0)
    return {
        "variant": result.variant,
        "final_nav": float(result.nav.iloc[-1]),
        "core": metrics,
        "relative_to_0050": relative,
        "monthly": profile,
        "trades": concentration,
        "bootstrap": bootstrap,
        "f1_criteria": criteria,
        "exposure": {
            "mean_positions": float(result.position_count.mean()),
            "mean_cash_ratio": float((result.cash / result.nav).mean()),
            "sessions_fully_in_cash": int((result.position_count == 0).sum()),
        },
        "costs": {
            "commission_twd": diagnostics["total_commission_twd"],
            "transaction_tax_twd": diagnostics["total_transaction_tax_twd"],
            "total_twd": costs,
            "share_of_capital": float(costs / capital) if capital else float("nan"),
            "share_of_gross_profit": (float(costs / gross_profit)
                                      if gross_profit > 0 else float("nan")),
            "buy_notional_twd": diagnostics["total_buy_notional_twd"],
            "sell_notional_twd": diagnostics["total_sell_notional_twd"],
            "annual_turnover_x_capital": (
                float(diagnostics["total_buy_notional_twd"] / capital
                      / (diagnostics["sessions"] / 252))
                if capital and diagnostics["sessions"] else float("nan")),
        },
        "execution": {
            "fills": diagnostics["fills"],
            "blocked_attempts": diagnostics["blocked_attempts"],
            "target_months_without_average_volume":
                diagnostics["target_months_without_average_volume"],
            "corporate_action_events_applied":
                diagnostics["corporate_action_events_applied"],
            "corporate_action_outcomes": diagnostics["corporate_action_outcomes"],
            "open_positions_at_end": diagnostics["open_positions_at_end"],
        },
        "decision_months": int(len(result.monthly)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id", default=RELEASE_ID)
    parser.add_argument("--draws", type=int, default=1000)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "reports/mom1_f1_backward_holdout.json")
    args = parser.parse_args()

    if args.output.exists():
        raise SystemExit(
            f"{args.output} 已存在。F1 只能開封一次；要重跑必須明確說明理由並改檔名，"
            "不得靜默覆寫既有的一次性結果。")

    descriptor = _assert_policy_allows(args.release_id)
    strategy_commit = _assert_contract_committed()

    inputs = load_momentum_release(args.release_id, root=ROOT)
    signal = compute_mom_6_1(inputs.adjusted_close)
    ledger = pd.read_csv(ROOT / LEDGER_PATH)
    ledger["event_date"] = pd.to_datetime(ledger["event_date"], errors="coerce")
    ledger = ledger[ledger["event_date"].notna()]

    benchmark = benchmark_nav(inputs, start=HOLDOUT_START, end=HOLDOUT_END)

    results = {}
    for variant in ("MOM-1A", "MOM-1B"):
        print(f"running {variant} ...", flush=True)
        outcome = simulate(inputs, variant=variant, signal=signal, ledger=ledger,
                           start=HOLDOUT_START, end=HOLDOUT_END)
        results[variant] = summarise(outcome, benchmark, args.draws)

    report = {
        "schema_version": 1,
        "study": "MOM-1 F1 backward holdout falsification test (SPEC 9.2)",
        "performance_inspected": True,
        "one_shot": True,
        "holdout_window": {"start": HOLDOUT_START, "end": HOLDOUT_END},
        "data_release_id": args.release_id,
        "release_usage_policy": descriptor.get("usage_policy"),
        "strategy_commit": strategy_commit,
        "strategy_spec_sha256": sha256_file(
            ROOT / "docs/SPEC_DATA_FOUNDATION_AND_MOMENTUM.md"),
        "verified_input_sha256": inputs.verified_input_sha256,
        "command": (f"python scripts/run_mom1_f1_holdout.py "
                    f"--release-id {args.release_id} --draws {args.draws}"),
        "benchmark": {
            "name": "0050 total return buy and hold",
            "final_nav": float(benchmark.iloc[-1]),
            "core": core_metrics(benchmark),
        },
        "conclusion_force": {
            "failed": "MOM-1 is falsified on clean data; formally rejected per SPEC 9.5",
            "passed": ("not falsified under the 2005-2014 market regime only; "
                       "this is NOT positive evidence, NOT a deployment claim, "
                       "and NOT transferable to the current regime"),
        },
        "regime_differences": (
            "holdout ran under +/-7% price limits, call auction, no day trading and "
            "no odd-lot session; the current market differs on all four (SPEC 9.2.2)"),
        "results": results,
        "n_trials_added": 2,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                      default=str) + "\n", encoding="utf-8")
    print(f"\nwritten: {args.output}")
    for variant, payload in results.items():
        verdict = "PASSED" if payload["f1_criteria"]["passed"] else "FAILED"
        print(f"{variant}: {verdict}")


if __name__ == "__main__":
    main()
