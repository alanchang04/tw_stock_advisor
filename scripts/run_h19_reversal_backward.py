"""H19：短期反轉（`rs20`）的 backward 複製（2008-01 ~ 2014-12）。

事前登記見 `research/HYPOTHESES.md` H19。**執行前不得增減。**

這是 2008–2014 僅存的乾淨 backward 候選
----------------------------------------
`research/EVIDENCE_MATRIX.md`：`rs20` 的 Outcome exposure = NO。
`stack_days` 已由 H18 用掉。**這一段用完就沒有了。**

依 M25：主檢定的形式必須與挑規格的證據一致
------------------------------------------
挑 20 日 horizon 的依據是 development 的 **rank IC**（−0.0195、t=−8.45），
**因此主要估計量也用 rank IC，不用線性 FM**。

H18 就是敗在這裡：用排序證據挑 horizon、卻用原始計數的線性迴歸當主檢定，
結果主檢定 t=0.20 未過，而事前列為描述性的同口徑 rank IC 卻是 t=2.94。

選 20 日不是挑好看的
--------------------
1. **文獻先驗**：短期反轉是全球最穩健的異常之一（Jegadeesh 1990、
   Lehmann 1990）。`rs20` 用 20 日過去報酬預測次月正是其標準形式。
   **這個先驗不是從我們的資料來的。**
2. **與 development 一致**：短天期（5／10／20 日）IC 全為負。

60 日 IC 在 development 是 **+0.0102**（符號相反）——那是另一個現象，
列為描述性，**不得因為 20 日不顯著就改用它**。
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
    build_forward_panel, cumulative_from_path, newey_west_se,
)
from research.inference import stationary_bootstrap_series_ci  # noqa: E402
from research.unified_panel import build_panel, month_end_sessions  # noqa: E402

BACKWARD_START = "2008-01-01"
BACKWARD_END = "2014-12-31"
RS_LOOKBACK = 20                 # 現行 rs20 的既有定義，**不得掃描**
NEWEY_WEST_LAGS = 6
DECILES = 10
MIN_CROSS_SECTION = 30
# development 對照值（2015–2026，factor_report_2026-07-19）
DEV_IC_20D = -0.0195
DEV_IC_60D = +0.0102


def monthly_rank_ic(signal: pd.DataFrame, target: pd.DataFrame,
                    eligible: pd.DataFrame) -> pd.Series:
    """逐月 Spearman rank IC。**這是 H19 的主要估計量。**"""
    values = []
    for date in signal.index:
        pair = pd.DataFrame({"s": signal.loc[date].where(eligible.loc[date]),
                             "f": target.loc[date]}).dropna()
        if len(pair) >= MIN_CROSS_SECTION:
            values.append(pair["s"].corr(pair["f"], method="spearman"))
    return pd.Series(values, index=None, dtype=float)


def summarise_ic(series: pd.Series, *, development: float) -> dict:
    se = newey_west_se(series, lags=NEWEY_WEST_LAGS)
    mean = float(series.mean())
    t_stat = mean / se if se and np.isfinite(se) else float("nan")
    boot = stationary_bootstrap_series_ci(series, mean_block=6)
    return {
        "months": int(series.size),
        "mean": round(mean, 5),
        "se": round(float(se), 5) if se else None,
        "t_stat": round(float(t_stat), 3),
        # 單尾檢定，H0: IC >= 0、對立為反轉（IC < 0）。
        # `share_below_zero` 是 bootstrap 平均落在零以下的**比例**＝信心，
        # **不是 p 值**；p 值是 1 − 它。初版把欄位命名成 p 值，會被讀反。
        "bootstrap_share_below_zero": round(float(boot["share_below_zero"]), 4),
        "one_sided_p_value": round(1.0 - float(boot["share_below_zero"]), 4),
        "bootstrap_ci95": [round(boot["ci95_low"], 5), round(boot["ci95_high"], 5)],
        "share_of_months_negative": round(float((series < 0).mean()), 4),
        "development_comparison": development,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "research/results/h19_reversal_backward.json")
    args = parser.parse_args()

    print("building panel ...", flush=True)
    panel = build_panel()
    decisions = pd.DatetimeIndex(
        [d for d in month_end_sessions(panel.sessions)
         if pd.Timestamp(BACKWARD_START) <= d <= pd.Timestamp(BACKWARD_END)])

    monthly_return = panel.adjusted.reindex(decisions).pct_change(fill_method=None)
    eligible = panel.eligible.reindex(decisions).fillna(False)
    log_size = panel.log_size.reindex(decisions)
    benchmark = monthly_return.where(eligible).mean(axis=1)
    decision_excess = monthly_return.sub(benchmark, axis=0).where(eligible)
    forward_excess = monthly_return.sub(benchmark, axis=0)      # M13

    signal = ((panel.adjusted / panel.adjusted.shift(RS_LOOKBACK) - 1.0)
              .where(panel.usable_signal_mask(RS_LOOKBACK))
              .reindex(decisions))

    # 主要估計量：次月（≈20 個交易日）的 rank IC
    primary = monthly_rank_ic(signal, forward_excess.shift(-1), eligible)
    # 描述性：3 個月（≈60 個交易日）——符號相反的另一個現象
    three_month = (panel.adjusted.reindex(decisions).shift(-3)
                   / panel.adjusted.reindex(decisions) - 1.0)
    three_month_excess = three_month.sub(
        three_month.where(eligible).mean(axis=1), axis=0)
    secondary = monthly_rank_ic(signal, three_month_excess, eligible)

    report = {
        "study": "H19 — short-term reversal (rs20) backward replication",
        "window": [BACKWARD_START, BACKWARD_END],
        "track": ("BACKWARD TEST. rs20 had Outcome exposure = NO on this segment "
                  "before this run (research/EVIDENCE_MATRIX.md). It was the LAST "
                  "such candidate; after this run 2008-2014 has none left."),
        "m25_compliance": (
            "the primary estimand is rank IC, the SAME form as the development "
            "evidence used to pick the 20-session horizon. H18 failed by picking a "
            "horizon from rank-IC evidence and then testing with a linear "
            "regression on a raw count."),
        "horizon_justification": (
            "short-term reversal is one of the most robust global anomalies "
            "(Jegadeesh 1990, Lehmann 1990) and rs20 predicting the next month is "
            "its standard form. The literature prior does NOT come from our data; "
            "development merely agrees with it (5/10/20-session IC all negative)."),
        "prior_expectation": (
            "development 20-session IC is -0.0195 (t=-8.45). Short-term reversal is "
            "usually STRONGER in volatile periods and 2008-2014 contains the crash, "
            "so IC <= -0.02 is expected. A POSITIVE result would be a failure to "
            "replicate a globally robust anomaly and should trigger implementation "
            "checks first (signal sign, adjustment, alignment)."),
        "signal_definition": f"adj / adj.shift({RS_LOOKBACK}) - 1",
        "decision_months": int(len(decisions)),
        "primary_null": "H0: rank IC >= 0 (one-sided; alternative is reversal)",
        "primary_trials": 1,
        "primary_estimand_rank_ic_20d": summarise_ic(primary, development=DEV_IC_20D),
        "descriptive_rank_ic_60d": summarise_ic(secondary, development=DEV_IC_60D),
    }

    audit = leave_one_out_audit(
        pd.DataFrame({"event_date": decisions[-primary.size:],
                      "value": primary.to_numpy()}), "value", by="year")
    report["leave_one_year_out"] = {
        "years": int(audit["groups"]),
        "survives": bool(audit["conclusion_survives_any_single_removal"]),
        "flips_sign_groups": int(audit["flips_sign_groups"]),
        "most_influential_year": audit["most_influential_group"],
        "mean_without_it": round(audit["mean_without_it"], 5),
    }

    # 描述性：線性 FM（H18 的教訓——這個**不是**主檢定）
    fm_panel = build_forward_panel(signal=signal, decision_frame=decision_excess,
                                   forward_frame=forward_excess,
                                   controls={"log_size": log_size},
                                   horizons=(1, 2, 3))
    linear = cumulative_from_path(fm_panel, factor="signal", horizons=(1,),
                                  lags=NEWEY_WEST_LAGS, controls=["log_size"])
    report["descriptive_linear_fm_1m"] = {
        "mean_pct": round(linear["cumulative"] * 100, 5),
        "t_stat": round(linear["t_stat"], 3),
        "note": "descriptive only; the primary estimand is rank IC (M25)",
    }

    # 描述性：十分位
    target = forward_excess.shift(-1)
    buckets = {d: [] for d in range(1, DECILES + 1)}
    for date in decisions:
        values = signal.loc[date].where(eligible.loc[date]).dropna()
        if len(values) < DECILES * 5:
            continue
        rank = pd.qcut(values.rank(method="first"), DECILES, labels=False) + 1
        for decile in range(1, DECILES + 1):
            hit = target.loc[date].reindex(values.index[rank == decile]).dropna()
            if not hit.empty:
                buckets[decile].append(float(hit.mean()))
    means = {d: round(float(np.mean(v)) * 100, 4) for d, v in buckets.items() if v}
    ordered = [means[d] for d in range(1, DECILES + 1)] if len(means) == DECILES else []
    report["descriptive_deciles"] = {
        "mean_pct_by_decile": means,
        "adjacent_decreases_out_of_9": (
            int(sum(1 for a, b in zip(ordered, ordered[1:]) if b <= a))
            if ordered else None),
        "top_minus_bottom_pct": (round(ordered[-1] - ordered[0], 4)
                                 if ordered else None),
        "note": ("reversal predicts a DECREASING profile, so count decreases. "
                 "Levels are not interpretable under M13; only the spread is."),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")

    p = report["primary_estimand_rank_ic_20d"]
    s = report["descriptive_rank_ic_60d"]
    lo = report["leave_one_year_out"]
    print(f"\n=== H19 主要估計量：20 日 rank IC（2008-2014） ===")
    print(f"  IC {p['mean']:+.5f}  t={p['t_stat']:+.2f}  "
          f"單尾 p={p['one_sided_p_value']:.4f}"
          f"（bootstrap 落在零以下 {p['bootstrap_share_below_zero']:.2%}）")
    print(f"  bootstrap CI95 {p['bootstrap_ci95']}  為負的月份 "
          f"{p['share_of_months_negative']:.1%}  ({p['months']} 個月)")
    print(f"  對照 development {p['development_comparison']:+.4f}")
    print(f"  逐年刪除變號 {lo['flips_sign_groups']}/{lo['years']} 組")
    print(f"\n描述性（不判決）：")
    print(f"  60 日 rank IC {s['mean']:+.5f} (t={s['t_stat']:+.2f})  "
          f"對照 development {s['development_comparison']:+.4f}")
    print(f"  線性 FM 次月 {report['descriptive_linear_fm_1m']['mean_pct']:+.4f}% "
          f"(t={report['descriptive_linear_fm_1m']['t_stat']:+.2f})")
    print(f"  十分位遞減 {report['descriptive_deciles']['adjacent_decreases_out_of_9']}/9"
          f"  D10-D1 {report['descriptive_deciles']['top_minus_bottom_pct']:+.4f}%")
    print(f"\nwritten: {args.output}")


if __name__ == "__main__":
    main()
