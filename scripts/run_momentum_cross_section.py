"""步驟 1／2：台股動能的橫斷面特徵化（2008-01 ~ 2026-08-13）。

事前登記見 `docs/PLAN_2026-08-16_CROSS_SECTIONAL_TURN.md`。**執行前不得增減。**

軌別
----
**historical mechanism / development。不是驗證，也不會變成驗證。**
動能族的乾淨歷史 holdout = 0（2008~2014 由 F1 開封兩次；2015~2026 是四個
形成窗候選的 IC 選擇集合，見 SPEC §1.2／§9.2.4）。唯一的真確認只有 forward。

事前登記
--------
| 項目 | 內容 |
|---|---|
| 主要估計量 | ``CUM6 = Σ β_k, k=1..6`` |
| 主要虛無假設 | ``H0: CUM6 <= 0`` |
| 主要訊號 | ``mom_12_1``（252 回看、20 跳過） |
| 次要訊號 | ``mom_6_1``（120 回看、20 跳過） |
| 主要規格 | Fama-MacBeth，控制 ``log_size`` |
| 次要規格 | 不控制（robustness） |
| 主要推論 | Newey-West lags = 6 |
| 穩健性 | stationary bootstrap，**不掃 lag 找顯著** |
| 前瞻報酬 | 依 M13 不得套用未來合格條件 |
| 描述性（不判決） | M+1…M+6 路徑、rank IC、十分位、逐年刪除 |

**主要試驗數 = 2**（兩個訊號各一個 CUM6）。步驟 2 另計 1 個。

必須隨結論出現的揭露
--------------------
``mom_12_1`` 雖有 Jegadeesh-Titman 的文獻先驗，但它**也是 SPEC §1.2 那張
2015~2026 IC 表的四個候選之一**，且 120 日 IC 是四個裡最差的（−0.0018）。
「因為文獻所以選它」不能洗掉「我們已經看過它」。

步驟 2：市場狀態
----------------
採 Cooper-Gutierrez-Hameed 式的**文獻凍結定義**：
**過去三年市場累積報酬非負為 UP，負為 DOWN**。
不用自創的「當月報酬正負」——那多開一個自由度，且衡量的是當月狀態
而非市場狀態。主要估計量 ``β_UP − β_DOWN``。
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

from research.episodes import leave_one_out_audit  # noqa: E402
from research.fama_macbeth import (  # noqa: E402
    DEFAULT_HORIZONS, build_forward_panel, cumulative_from_path,
    fama_macbeth, newey_west_se, response_path,
)
from research.inference import (  # noqa: E402
    multiple_testing_budget, stationary_bootstrap_series_ci,
)
from research.unified_panel import (  # noqa: E402
    build_panel, month_end_sessions, momentum_signal,
)

NEWEY_WEST_LAGS = 6                 # 事前固定 = 最長領先期，不得為 p 值調整
BOOTSTRAP_MEAN_BLOCK = 6            # 期（月）
SIGNALS = {"mom_12_1": (252, 20), "mom_6_1": (120, 20)}
PRIMARY_SIGNAL = "mom_12_1"
DECILES = 10
# CGH 式市場狀態：過去三年累積市場報酬
MARKET_STATE_LOOKBACK_MONTHS = 36


def monthly_frames(panel, decisions: pd.DatetimeIndex):
    """月頻報酬、決策時遮罩與規模控制。"""
    month_close = panel.adjusted.reindex(decisions)
    monthly_return = month_close.pct_change(fill_method=None)
    eligible = panel.eligible.reindex(decisions).fillna(False)
    log_size = panel.log_size.reindex(decisions)
    # 決策時的橫斷面成員資格套遮罩；前瞻報酬**不套**（M13）
    benchmark = monthly_return.where(eligible).mean(axis=1)
    decision_excess = monthly_return.sub(benchmark, axis=0).where(eligible)
    forward_excess = monthly_return.sub(benchmark, axis=0)
    return monthly_return, decision_excess, forward_excess, log_size, benchmark


def rank_ic(signal: pd.DataFrame, forward: pd.DataFrame,
            eligible: pd.DataFrame) -> dict:
    """逐月 Spearman rank IC（描述性，不判決）。"""
    values = []
    target = forward.shift(-1)
    for date in signal.index:
        s = signal.loc[date].where(eligible.loc[date])
        f = target.loc[date]
        pair = pd.DataFrame({"s": s, "f": f}).dropna()
        if len(pair) >= 30:
            values.append(pair["s"].corr(pair["f"], method="spearman"))
    series = pd.Series(values, dtype=float)
    se = newey_west_se(series, lags=NEWEY_WEST_LAGS)
    return {"months": int(series.size), "mean": round(float(series.mean()), 5),
            "t_stat": round(float(series.mean() / se), 3)
            if se and np.isfinite(se) else None}


def decile_profile(signal: pd.DataFrame, forward: pd.DataFrame,
                   eligible: pd.DataFrame) -> dict:
    """十分位平均超額（描述性，不判決）。單調性比 D10−D1 有資訊得多。"""
    target = forward.shift(-1)
    buckets: dict[int, list[float]] = {d: [] for d in range(1, DECILES + 1)}
    for date in signal.index:
        s = signal.loc[date].where(eligible.loc[date]).dropna()
        f = target.loc[date]
        if len(s) < DECILES * 5:
            continue
        rank = pd.qcut(s.rank(method="first"), DECILES, labels=False) + 1
        for d in range(1, DECILES + 1):
            hit = f.reindex(s.index[rank == d]).dropna()
            if not hit.empty:
                buckets[d].append(float(hit.mean()))
    means = {d: round(float(np.mean(v)) * 100, 4) for d, v in buckets.items() if v}
    if len(means) == DECILES:
        ordered = [means[d] for d in range(1, DECILES + 1)]
        monotone = int(sum(1 for a, b in zip(ordered, ordered[1:]) if b >= a))
    else:
        monotone = None
    return {"mean_pct_by_decile": means,
            "adjacent_increases_out_of_9": monotone,
            "top_minus_bottom_pct": (round(means[DECILES] - means[1], 4)
                                     if len(means) == DECILES else None)}


def market_state(benchmark: pd.Series) -> pd.Series:
    """CGH 式：過去 36 個月市場累積報酬非負為 UP。**文獻凍結定義，不自創。**

    ``benchmark`` 是等權市場月報酬。實測結果見報告：這個定義在台股
    2011~2026 上**產生 0 個 DOWN 月**（36 個月累積最低 +41.3%），
    因此估計量無定義——那是真實性質，不是實作錯誤。
    """
    growth = (1.0 + benchmark.fillna(0.0))
    cumulative = growth.rolling(MARKET_STATE_LOOKBACK_MONTHS).apply(np.prod, raw=True)
    state = pd.Series(np.where(cumulative >= 1.0, "UP", "DOWN"),
                      index=benchmark.index)
    return state.where(cumulative.notna())


def study_signal(name: str, panel, decisions, frames) -> dict:
    monthly_return, decision_excess, forward_excess, log_size, benchmark = frames
    lookback, skip = SIGNALS[name]
    # 形成窗內含跨停牌大跳空者一律排除（見 unified_panel.usable_signal_mask）
    signal = (momentum_signal(panel.adjusted, lookback=lookback, skip=skip)
              .where(panel.usable_signal_mask(lookback))
              .reindex(decisions))
    eligible = panel.eligible.reindex(decisions).fillna(False)

    fm_panel = build_forward_panel(signal=signal, decision_frame=decision_excess,
                                   forward_frame=forward_excess,
                                   controls={"log_size": log_size})
    entry = {"signal": name, "lookback_sessions": lookback, "skip_sessions": skip,
             "observations": int(len(fm_panel)),
             "decision_months": int(fm_panel["period"].nunique())}

    for spec, controls in (("size_controlled", ["log_size"]), ("raw", [])):
        cumulative = cumulative_from_path(fm_panel, factor="signal",
                                          lags=NEWEY_WEST_LAGS, controls=controls)
        path = response_path(fm_panel, factor="signal", lags=NEWEY_WEST_LAGS,
                             controls=controls)
        block = {
            "primary_estimand_CUM6": {
                "periods": cumulative["periods"],
                "periods_dropped": cumulative["periods_dropped"],
                "mean_pct": round(cumulative["cumulative"] * 100, 4),
                "se_pct": round(cumulative["se"] * 100, 4),
                "t_stat": round(cumulative["t_stat"], 3),
                "p_value": round(cumulative["p_value"], 4),
            },
            "descriptive_path": [
                {"horizon_months": int(r["horizon_months"]),
                 "mean_pct": round(r["mean"] * 100, 4),
                 "t_stat": round(r["t_stat"], 3)}
                for _, r in path.iterrows()
            ],
        }
        if spec == "size_controlled":
            boot = stationary_bootstrap_series_ci(
                cumulative["series"], mean_block=BOOTSTRAP_MEAN_BLOCK)
            block["robustness_stationary_bootstrap"] = {
                "ci95_low_pct": round(boot["ci95_low"] * 100, 4),
                "ci95_high_pct": round(boot["ci95_high"] * 100, 4),
                "share_below_zero": round(boot["share_below_zero"], 4),
                "note": "robustness only; primary inference remains NW(6)",
            }
            audit = leave_one_out_audit(
                pd.DataFrame({"event_date": cumulative["series"].index,
                              "value": cumulative["series"].to_numpy()}),
                "value", by="year")
            block["leave_one_year_out"] = {
                "years": int(audit["groups"]),
                "survives": bool(audit["conclusion_survives_any_single_removal"]),
                "flips_sign_groups": int(audit["flips_sign_groups"]),
                "most_influential_year": audit["most_influential_group"],
                "mean_without_it_pct": round(audit["mean_without_it"] * 100, 4),
            }
            entry["cum6_series"] = {str(k.date()): round(float(v) * 100, 5)
                                    for k, v in cumulative["series"].items()}
        entry[spec] = block

    entry["rank_ic"] = rank_ic(signal, forward_excess, eligible)
    entry["deciles"] = decile_profile(signal, forward_excess, eligible)
    return entry


def study_market_state(name: str, panel, decisions, frames) -> dict:
    """步驟 2：``β_UP − β_DOWN``，市場狀態採 CGH 式文獻定義。"""
    monthly_return, decision_excess, forward_excess, log_size, benchmark = frames
    lookback, skip = SIGNALS[name]
    signal = (momentum_signal(panel.adjusted, lookback=lookback, skip=skip)
              .where(panel.usable_signal_mask(lookback))
              .reindex(decisions))
    fm_panel = build_forward_panel(signal=signal, decision_frame=decision_excess,
                                   forward_frame=forward_excess,
                                   controls={"log_size": log_size})
    cumulative = cumulative_from_path(fm_panel, factor="signal",
                                      lags=NEWEY_WEST_LAGS,
                                      controls=["log_size"])
    series = cumulative["series"]
    state = market_state(benchmark).reindex(series.index)

    groups = {}
    for label in ("UP", "DOWN"):
        subset = series[state == label]
        if subset.size < 12:
            groups[label] = {"periods": int(subset.size), "insufficient": True}
            continue
        se = newey_west_se(subset, lags=NEWEY_WEST_LAGS)
        groups[label] = {
            "periods": int(subset.size),
            "mean_pct": round(float(subset.mean()) * 100, 4),
            "se_pct": round(float(se) * 100, 4) if se else None,
            "t_stat": round(float(subset.mean() / se), 3)
            if se and np.isfinite(se) else None,
        }

    difference = None
    if all(not groups[k].get("insufficient") for k in ("UP", "DOWN")):
        up, down = series[state == "UP"], series[state == "DOWN"]
        gap = float(up.mean() - down.mean())
        # 兩組互斥，變異數相加；各組用自己的 Newey-West 標準誤
        se = float(np.hypot(newey_west_se(up, lags=NEWEY_WEST_LAGS),
                            newey_west_se(down, lags=NEWEY_WEST_LAGS)))
        difference = {"beta_up_minus_down_pct": round(gap * 100, 4),
                      "se_pct": round(se * 100, 4),
                      "t_stat": round(gap / se, 3) if se else None}

    return {
        "signal": name,
        "state_definition": ("Cooper-Gutierrez-Hameed style: UP if the trailing "
                             f"{MARKET_STATE_LOOKBACK_MONTHS}-month cumulative "
                             "equal-weight market return is non-negative"),
        "months_by_state": {k: int((state == k).sum()) for k in ("UP", "DOWN")},
        "months_undefined": int(state.isna().sum()),
        "by_state": groups,
        "primary_estimand_difference": difference,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "research/results/momentum_cross_section.json")
    args = parser.parse_args()

    print("building unified panel ...", flush=True)
    panel = build_panel()
    decisions = month_end_sessions(panel.sessions)
    frames = monthly_frames(panel, decisions)
    print(f"  {len(panel.sessions)} sessions, {panel.close.shape[1]} securities, "
          f"{len(decisions)} decision months", flush=True)

    report = {
        "study": "step 1/2 — cross-sectional momentum characterization",
        "track": ("historical mechanism / DEVELOPMENT. Not a validation and cannot "
                  "become one: the momentum family has zero clean historical "
                  "holdout (SPEC §1.2/§9.2.4). Only forward can confirm."),
        "panel": {"start": str(panel.sessions.min().date()),
                  "end": str(panel.sessions.max().date()),
                  "sessions": int(len(panel.sessions)),
                  "securities": int(panel.close.shape[1]),
                  "decision_months": int(len(decisions)),
                            "seam_audit": "reports/STEP0_PRICE_SEAM_AUDIT.md (verdict: clean)",
                  "jump_repair": panel.jump_repair},
        "preregistration": {
            "primary_estimand": "CUM6 = sum of beta_k over k=1..6",
            "primary_null": "H0: CUM6 <= 0",
            "primary_signal": PRIMARY_SIGNAL,
            "primary_specification": "Fama-MacBeth controlling log_size",
            "primary_inference": f"Newey-West lags={NEWEY_WEST_LAGS}",
            "primary_trials_step1": 2,
            "primary_trials_step2": 1,
        },
        "mandatory_disclosure": (
            "mom_12_1 has a Jegadeesh-Titman prior, but it is ALSO one of the four "
            "formation-window candidates already examined on 2015~2026 in SPEC §1.2, "
            "where its 120-day IC was the worst of the four (-0.0018). "
            "'We picked it because of the literature' does not undo 'we have seen it'."),
        "prior_expectation": (
            "unfavourable: MOM-1's cross-sectional rank IC on 2008~2014 was "
            "-0.0003 (t=-0.017) with non-monotone deciles. A suddenly beautiful "
            "result should trigger suspicion of the seam first, not celebration."),
        "step1_signals": [],
    }

    for name in SIGNALS:
        print(f"  {name} ...", flush=True)
        report["step1_signals"].append(study_signal(name, panel, decisions, frames))

    print(f"  market state ({PRIMARY_SIGNAL}) ...", flush=True)
    report["step2_market_state"] = study_market_state(
        PRIMARY_SIGNAL, panel, decisions, frames)

    # 實際顯著的主要檢定數（不得寫死；步驟 2 未能執行故不計入分子）
    significant = sum(
        1 for e in report["step1_signals"]
        if e["size_controlled"]["primary_estimand_CUM6"]["p_value"] < 0.05)
    report["multiple_testing"] = multiple_testing_budget(cells=3,
                                                         significant=significant)
    report["multiple_testing"]["note"] = (
        "cells = 2 step-1 primaries + 1 step-2 primary. Step 2 could not be "
        "executed (zero DOWN months), so it contributes a cell to the budget "
        "but no result.")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")

    print("\n=== 步驟 1：主要估計量 CUM6（控制規模） ===")
    for entry in report["step1_signals"]:
        c = entry["size_controlled"]["primary_estimand_CUM6"]
        b = entry["size_controlled"]["robustness_stationary_bootstrap"]
        lo = entry["size_controlled"]["leave_one_year_out"]
        print(f"  {entry['signal']:10s} CUM6 {c['mean_pct']:+7.3f}%  "
              f"t={c['t_stat']:+6.2f}  p={c['p_value']:.4f}  "
              f"({c['periods']} 期，丟 {c['periods_dropped']})")
        print(f"             bootstrap CI95 [{b['ci95_low_pct']:+.3f}%, "
              f"{b['ci95_high_pct']:+.3f}%]  逐年刪除變號 {lo['flips_sign_groups']} 組")
        print(f"             路徑 " + " ".join(
            f"{p['mean_pct']:+.3f}" for p in entry["size_controlled"]["descriptive_path"]))
        print(f"             rank IC {entry['rank_ic']['mean']:+.5f} "
              f"(t={entry['rank_ic']['t_stat']})  "
              f"十分位遞增 {entry['deciles']['adjacent_increases_out_of_9']}/9  "
              f"D10-D1 {entry['deciles']['top_minus_bottom_pct']:+.3f}%")
    ms = report["step2_market_state"]
    print(f"\n=== 步驟 2：市場狀態（{ms['signal']}） ===")
    print(f"  UP {ms['months_by_state']['UP']} 月 / DOWN "
          f"{ms['months_by_state']['DOWN']} 月 / 未定義 {ms['months_undefined']}")
    for label in ("UP", "DOWN"):
        g = ms["by_state"][label]
        if g.get("insufficient"):
            print(f"  {label:5s} 期數不足（{g['periods']}）")
        else:
            print(f"  {label:5s} CUM6 {g['mean_pct']:+7.3f}%  t={g['t_stat']}")
    d = ms["primary_estimand_difference"]
    if d:
        print(f"  UP−DOWN {d['beta_up_minus_down_pct']:+.3f}%  t={d['t_stat']}")
    print(f"\nwritten: {args.output}")


if __name__ == "__main__":
    main()
