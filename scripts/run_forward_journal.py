"""Forward journal：每月一次，記錄 H11 與 H16 在**新資料**上的表現。

    python scripts/run_forward_journal.py          # 每月跑這一行就好

## 這個腳本的責任是「不要讓事情改變」

基本面鏈（H11~H16）的歷史研究已經結束。H12／H13 否決、H16 inconclusive、
H11 歷史證據穩健但仍需獨立確認。剩下唯一該做的事是**照凍結的定義累積新資料**。

因此本腳本刻意**沒有任何參數**可調。它只做四件事：

1. 以**完全凍結**的定義計算每個新決策月的兩個數字
2. **append-only** 寫進 journal，已存在的月份不重算也不覆寫
3. 每次執行都比對**定義指紋**；指紋變了就中止，不寫入
4. 用 **always-valid 信賴序列**呈現累積證據——可以每月看而不破壞錯誤率

## 為什麼要 always-valid 而不是每月看 p 值

固定樣本 p 值只在「事先講好看幾次」時有效。這個 journal 的本質是
每月看一次、看很多年；用固定樣本 p<0.05 判斷，光是反覆偷看
就會讓型一錯誤率遠高於 5%（`tests/test_sequential.py` 有對照組實測）。

## 依 M18：看過就變成 development 資料

**若哪天看了 journal 之後決定改模型／改定義，該日之前的所有 forward 資料
立刻變成新模型的 development 集**，新模型的乾淨 OOS 從那天之後重新起算。
所以最理想的狀態是：每月跑一次，什麼都不改。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.momentum import (  # noqa: E402
    ENTRY_TOP_FRAC, FORMATION_LOOKBACK, FORMATION_SKIP,
)
from research.sequential import (  # noqa: E402
    SequentialPlan, confidence_sequence, planning_horizon,
)
import scripts.run_h12_institutional_confirmation as H12  # noqa: E402
import scripts.run_h16_momentum_confirmation as H16  # noqa: E402

# ── 事前登記、開始累積後不得更動 ────────────────────────────────────
FORWARD_START = "2026-08-14"      # H16 登記日；此後的決策月才算 forward
JOURNAL_PATH = ROOT / "reports/forward_journal.json"

# 逐期噪音規模，由**歷史**執行反推後寫死（不得從 forward 資料重估，
# 否則 sigma 會隨結果漂移）：
#   H16  SE 1.1009% x sqrt(130) = 12.55
#   H11  SE 0.1951% x sqrt(130) =  2.22
SIGMA_H16 = 12.55
SIGMA_H11 = 2.22
PLAN_H16 = SequentialPlan(sigma=SIGMA_H16, alpha=0.05, tightest_at=60)
PLAN_H11 = SequentialPlan(sigma=SIGMA_H11, alpha=0.05, tightest_at=60)

# 歷史點估計，只用於規劃數字（不是證據）
HISTORICAL_H16_DELTA = 1.8639

DEFINITION_FILES = (
    "research/momentum.py",
    "research/sequential.py",
    "scripts/run_h12_institutional_confirmation.py",
    "scripts/run_h16_momentum_confirmation.py",
)


def definition_fingerprint() -> str:
    """凍結定義的指紋。變了就代表定義漂移，journal 必須拒絕續寫。"""
    digest = hashlib.sha256()
    for name in DEFINITION_FILES:
        digest.update(name.encode())
        digest.update((ROOT / name).read_bytes())
    for value in (H16.PRIMARY_HORIZON, H16.REVENUE_MIN_YOY, ENTRY_TOP_FRAC,
                  FORMATION_SKIP, FORMATION_LOOKBACK, H12.CONFIRMATION_WINDOW,
                  H12.CONFIRMATION_LOTS):
        digest.update(repr(value).encode())
    return digest.hexdigest()


def monthly_records(snapshot: Path) -> list[dict]:
    """用 H16 的凍結面板算出每個決策月的兩個數字。

    同一份面板同時給出 H11 與 H16：
      H11 = 全體 Rev+ 的平均超額（該月）
      H16 = 控制規模後 Mom+ 相對 Mom− 的係數（該月）
    """
    panel = H16.build_panel(snapshot, minimum_yoy=H16.REVENUE_MIN_YOY,
                            horizon=H16.PRIMARY_HORIZON)
    if panel.empty:
        return []

    records = []
    for decision, group in panel.groupby("decision"):
        data = group.dropna(subset=["excess", "confirm", "log_size"])
        if len(data) < 30 or data["confirm"].nunique() < 2:
            continue
        design = np.column_stack([np.ones(len(data)),
                                  data["confirm"].to_numpy(),
                                  data["log_size"].to_numpy()])
        if np.linalg.matrix_rank(design) < design.shape[1]:
            continue
        solution, *_ = np.linalg.lstsq(design, data["excess"].to_numpy(), rcond=None)
        records.append({
            "decision_date": str(pd.Timestamp(decision).date()),
            "h11_revenue_plus_excess_pct": round(float(data["excess"].mean()) * 100, 4),
            "h16_delta_signal_pct": round(float(solution[1]) * 100, 4),
            "revenue_plus_count": int(len(data)),
            "momentum_plus_count": int(data["confirm"].sum()),
        })
    return records


def sequential_block(values: list, plan: SequentialPlan) -> dict:
    frame = confidence_sequence(values, plan)
    if frame.empty:
        return {"months": 0, "note": "no forward observations yet"}
    last = frame.iloc[-1]
    return {
        "months": int(last["n"]),
        "running_mean_pct": round(float(last["mean"]), 4),
        "always_valid_ci95": [round(float(last["lower"]), 4),
                              round(float(last["upper"]), 4)],
        "excludes_zero_now": bool(last["excludes_zero"]),
        "ever_excluded_zero": bool(frame["excludes_zero"].any()),
        "first_month_excluding_zero": (
            int(frame.loc[frame["excludes_zero"], "n"].iloc[0])
            if frame["excludes_zero"].any() else None),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=ROOT / "data/research")
    parser.add_argument("--journal", type=Path, default=JOURNAL_PATH)
    args = parser.parse_args()

    fingerprint = definition_fingerprint()
    existing = (json.loads(args.journal.read_text(encoding="utf-8"))
                if args.journal.exists() else None)

    if existing and existing.get("definition_fingerprint") != fingerprint:
        raise SystemExit(
            "定義指紋不符：凍結的定義被改動過。\n"
            f"  journal: {existing.get('definition_fingerprint')}\n"
            f"  現在   : {fingerprint}\n"
            "依 M18，改定義之後先前累積的 forward 資料即成為新模型的 development "
            "集，不能續寫同一本 journal。請開新 journal 並在登記簿說明。")

    print("computing frozen panel ...", flush=True)
    computed = monthly_records(args.snapshot)
    forward = [r for r in computed if r["decision_date"] >= FORWARD_START]

    kept = {r["decision_date"]: r for r in (existing or {}).get("observations", [])}
    added = [r for r in forward if r["decision_date"] not in kept]
    # append-only：既有月份一律保留原值，不因重算而改寫
    for record in added:
        kept[record["decision_date"]] = record
    observations = [kept[key] for key in sorted(kept)]

    h16_values = [r["h16_delta_signal_pct"] for r in observations]
    h11_values = [r["h11_revenue_plus_excess_pct"] for r in observations]

    journal = {
        "study": "forward journal for H11 (revenue) and H16 (momentum confirmation)",
        "forward_start": FORWARD_START,
        "definition_fingerprint": fingerprint,
        "frozen_parameters": {
            "primary_horizon_sessions": H16.PRIMARY_HORIZON,
            "revenue_min_yoy": H16.REVENUE_MIN_YOY,
            "momentum_entry_fraction": ENTRY_TOP_FRAC,
            "formation_skip": FORMATION_SKIP,
            "formation_lookback": FORMATION_LOOKBACK,
        },
        "inference": {
            "method": "always-valid normal-mixture confidence sequence",
            "why": ("a monthly journal is inspected many times; fixed-sample p-values "
                    "are only valid for a pre-committed number of looks"),
            "alpha": PLAN_H16.alpha,
            "tightest_at_months": PLAN_H16.tightest_at,
            "sigma_h16": SIGMA_H16,
            "sigma_h11": SIGMA_H11,
            "sigma_source": "back-solved from the historical runs; frozen, never re-estimated",
        },
        "h16_momentum_confirmation": sequential_block(h16_values, PLAN_H16),
        "h11_revenue_plus": sequential_block(h11_values, PLAN_H11),
        "planning_only_not_evidence": planning_horizon(HISTORICAL_H16_DELTA, SIGMA_H16),
        "observations": observations,
        "m18_reminder": ("inspecting this journal and then changing the model turns all "
                         "prior forward data into development data for the new model"),
    }

    args.journal.parent.mkdir(parents=True, exist_ok=True)
    args.journal.write_text(json.dumps(journal, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")

    print(f"\nforward months recorded: {len(observations)}  (new this run: {len(added)})")
    if not observations:
        latest = max((r["decision_date"] for r in computed), default="n/a")
        print(f"  尚無 forward 觀測。資料最後一個決策月 = {latest}，"
              f"forward 自 {FORWARD_START} 起算。")
        print("  這是正確的——forward 需要時間累積，不是現在就該有數字。")
    else:
        block = journal["h16_momentum_confirmation"]
        print(f"  H16 running mean = {block['running_mean_pct']:+.4f}%  "
              f"always-valid CI95 = {block['always_valid_ci95']}  "
              f"excludes zero = {block['excludes_zero_now']}")
    print(f"\nwritten: {args.journal}")


if __name__ == "__main__":
    main()
