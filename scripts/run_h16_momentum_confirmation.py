"""H16：基本面條件下的價格動能（登記見 `research/HYPOTHESES.md` H16）。

**問題**：在同樣有營收改善的公司裡，價格已經呈現中期強勢者，
是否比尚未呈現強勢者有更高的未來超額報酬？

    Δ_signal    = E[R | Rev+, Mom+] − E[R | Rev+, Mom−]      ← 主要判準
    Δ_portfolio = E[R | Rev+, Mom+] − E[R | Rev+]            ← 只報告

## 與 H12 的三個結構差異

1. **不需要 landmark。** `mom_6_1` 是**過去**報酬，決策日當天就完全已知，
   不存在 H12 那種「用之後 20 天決定分組、又把那 20 天算進報酬」的前視。
   因此前瞻報酬自 **d+1**（T+1）起算即可。

2. **不需要 common-coverage 限制。** H16 只用價量與月營收，
   不碰法人資料，所以 M19 的 TPEx 2018 斷點與本假說無關；
   樣本用月營收可得的完整期間。為了與 H12 對照，另報 2018+ 的切片
   （**標示 exploratory**，依 §8.3 未預先登記的切片不得決定採用）。

3. **規模控制量在決策日 d。** 與 H12 修正後一致：
   `mom_6_1` 與成交量都在 d 之前，控制變數也必須是 pre-treatment。

## 結論效力（登記已寫死，這裡再重申一次）

`mom_6_1` 的形成窗是**看過 2015~2026 的 IC 表**從四個候選挑出來的
（SPEC §7.2），而 2008~2014 已於 2026-08-14 耗盡。因此本輪：

    歷史資料 → falsification / development evidence only
    forward  → 唯一的 confirmatory evidence

**即使跑出漂亮的數字，也只能宣稱「未被否證」，不得稱為已驗證。**

試驗次數：4（條件迴歸 × {未控制, 控制規模} × {多空, 只做多}），事前登記。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.strategy import apply_total_return_adjustment, split_adjust  # noqa: E402
from research.fama_macbeth import newey_west_se  # noqa: E402
from research.momentum import ENTRY_TOP_FRAC, compute_mom_6_1  # noqa: E402
import scripts.run_h04_h06_event_study as H04  # noqa: E402
import scripts.run_h12_institutional_confirmation as H12  # noqa: E402

PRIMARY_HORIZON = 60          # 交易日；依 M12 事前指定
REVENUE_MIN_YOY = 0.0         # 主要規格；>20% 列 robustness
NEWEY_WEST_LAGS = 6
COMPARABILITY_START = "2018-01-01"    # 只為了與 H12 對照，標 exploratory


def build_panel(snapshot: Path, *, minimum_yoy: float, horizon: int) -> pd.DataFrame:
    prices = pd.read_parquet(snapshot / "prices.parquet",
                             columns=["stock_id", "trade_date", "close", "turnover"])
    prices["stock_id"] = prices["stock_id"].astype(str)
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    closes = prices.pivot(index="trade_date", columns="stock_id", values="close").sort_index()
    turnover = prices.pivot(index="trade_date", columns="stock_id", values="turnover").sort_index()

    dividends = pd.read_parquet(snapshot / "dividend_events.parquet")
    dividends["stock_id"] = dividends["stock_id"].astype(str)
    dividends["ex_date"] = pd.to_datetime(dividends["ex_date"])
    adjusted = apply_total_return_adjustment(closes.apply(split_adjust), dividends)

    # 訊號定義完全沿用 MOM-1，不重新選形成窗
    signal = compute_mom_6_1(adjusted)
    mask = H04.liquid_universe_mask(closes, turnover)
    sessions = pd.DatetimeIndex(closes.index)
    size = np.log1p(turnover.rolling(20).mean())

    last_usable = sessions[-(horizon + 2)]
    decisions = H12.monthly_decision_dates(sessions, sessions[0], last_usable)
    H12.revenue_positive.cache = H12.load_revenue(snapshot)

    rows = []
    for decision in decisions:
        eligible = [s for s in closes.columns if bool(mask.at[decision, s])]
        winners = H12.revenue_positive(snapshot, decision, minimum_yoy) & set(eligible)
        # 訊號必須在決策日有值，缺值不得補 0（SPEC §7.1.7）
        values = signal.loc[decision, [s for s in sorted(winners)
                                       if s in signal.columns]].dropna()
        if len(values) < 30:
            continue

        # Mom+ = Revenue+ 合格母體內前 10%，名額用 floor(n x 10%)，與 MOM-1 同一條規則
        cutoff = math.floor(len(values) * ENTRY_TOP_FRAC)
        if cutoff < 1:
            continue
        strongest = set(values.nlargest(cutoff).index)

        entry_position = sessions.get_loc(decision) + 1
        exit_position = entry_position + horizon
        if exit_position >= len(sessions):
            continue
        entry, exit_ = sessions[entry_position], sessions[exit_position]

        ids = [s for s in values.index if s in adjusted.columns]
        forward = (adjusted.loc[exit_, ids] / adjusted.loc[entry, ids] - 1.0)
        universe = [s for s in eligible if s in adjusted.columns]
        reference = float((adjusted.loc[exit_, universe]
                           / adjusted.loc[entry, universe] - 1.0).mean(skipna=True))
        rows.append(pd.DataFrame({
            "decision": decision,
            "stock_id": ids,
            # 欄名沿用 H12 的 "confirm"，好讓兩者共用同一組統計函式
            "confirm": [1.0 if s in strongest else 0.0 for s in ids],
            "excess": (forward - reference).to_numpy(),
            "log_size": size.loc[decision, ids].to_numpy(),
        }))

    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def summarise(panel: pd.DataFrame) -> dict:
    return {
        "breadth": H12.breadth(panel),
        "size_controlled": H12.conditional_regression(panel, controls=True),
        "uncontrolled": H12.conditional_regression(panel, controls=False),
        "two_by_two": H12.two_by_two(panel),
        "window": {"first_decision": str(panel["decision"].min().date()),
                   "last_decision": str(panel["decision"].max().date())},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "research/results/h16_momentum_confirmation.json")
    args = parser.parse_args()

    report = {
        "study": "H16 price-momentum confirmation conditional on revenue improvement",
        "registration": "research/HYPOTHESES.md H16",
        "primary_estimand": "E[R | Rev+, Mom+] - E[R | Rev+, Mom-]",
        "primary_horizon_sessions": PRIMARY_HORIZON,
        "momentum_definition": ("mom_6_1 = adjusted_close[t-20]/adjusted_close[t-120] - 1, "
                                "top floor(n x 10%) within the Revenue+ eligible universe; "
                                "identical to MOM-1, formation window NOT re-selected"),
        "evidence_status": {
            "historical": "falsification / development evidence ONLY",
            "confirmatory": "forward period after 2026-08-14 only",
            "reason": ("mom_6_1's formation window was chosen by inspecting the "
                       "2015-2026 IC table (SPEC 7.2) and the 2008-2014 holdout was "
                       "consumed on 2026-08-14; no clean historical ground remains"),
        },
        "trials_registered": 4,
        "specifications": {},
    }

    for label, minimum_yoy in (("primary_rev_yoy_gt_0", REVENUE_MIN_YOY),
                               ("robustness_rev_yoy_gt_20", 20.0)):
        print(f"building panel: {label} ...", flush=True)
        panel = build_panel(args.snapshot, minimum_yoy=minimum_yoy,
                            horizon=PRIMARY_HORIZON)
        if panel.empty:
            report["specifications"][label] = {"months": 0}
            continue
        entry = summarise(panel)
        # 與 H12 同期的對照切片（exploratory，不決定採用）
        comparable = panel[panel["decision"] >= pd.Timestamp(COMPARABILITY_START)]
        if len(comparable):
            entry["exploratory_2018_onwards_for_h12_comparison"] = {
                "note": "EXPLORATORY slice; not a registered specification (SPEC 8.3)",
                **summarise(comparable),
            }
        report["specifications"][label] = entry

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                      default=str) + "\n", encoding="utf-8")
    print(f"\nwritten: {args.output}")


if __name__ == "__main__":
    main()
