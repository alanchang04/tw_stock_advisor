"""H18：多頭排列持續天數的 backward 複製（2008-01 ~ 2014-12）。

事前登記見 `research/HYPOTHESES.md` H18。**執行前不得增減。**

為什麼這一段對這個訊號是乾淨的
------------------------------
`research/EVIDENCE_MATRIX.md` 的稽核：`stack_days` 在 2008–2014 的
**Outcome exposure = NO**——從未看過它與未來報酬的關係。
Feature exposure = YES，但那只是 2006–2014 量過它與 `mom_6_1` 的訊號
重疊度（+0.163）。**訊號之間相關不會讓未來報酬被看到。**

必須隨結論揭露的一件事
----------------------
主要 horizon 取 60 個交易日（≈3 個月），是**看著 2015–2026 的
`factor_report_2026-07-19` 選的**（該段 IC 隨 horizon 單調上升，
60 日 IC +0.0329、ICIR +0.469）。

這是**合法的規格選擇**——2015–2026 是 development、2008–2014 是未看過的
backward 集合，等於一組時間順序相反的 train/test。
**但不得寫成「事前就知道 60 日」。**

事前預期
--------
2008–2014 含 2008 崩盤（等權 −47.3%）與 2009 反彈（+123.5%），
**趨勢持續類訊號在劇烈反轉的 régime 下通常較差**，因此預期效果
**小於** development 期的量級。若一樣強甚至更強，
**第一反應是懷疑實作**（尤其 `stack_days` 的計算與還原）。
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
    build_forward_panel, cumulative_from_path, newey_west_se, response_path,
)
from research.inference import stationary_bootstrap_series_ci  # noqa: E402
from research.unified_panel import build_panel, month_end_sessions  # noqa: E402

BACKWARD_START = "2008-01-01"
BACKWARD_END = "2014-12-31"
# 主要估計量：3 個月 ≈ 60 個交易日。**來源見模組 docstring，必須揭露。**
PRIMARY_HORIZONS = (1, 2, 3)
DESCRIPTIVE_HORIZONS = (1, 2, 3, 4, 5, 6)
NEWEY_WEST_LAGS = 6
DECILES = 10
# 現行策略的既有定義，**不得掃描**
MA_FAST, MA_MID, MA_SLOW = 5, 20, 60


def consecutive_true(flags: pd.DataFrame) -> pd.DataFrame:
    """逐欄計算「至當列為止連續 True 的天數」。與 agent/strategy.py 同一個定義。"""
    cumulative = flags.cumsum()
    reset = cumulative.where(~flags).ffill().fillna(0.0)
    return (cumulative - reset).where(flags, 0.0)


def trend_stack_days(adjusted: pd.DataFrame) -> pd.DataFrame:
    """`MA5 > MA20 > MA60` 連續成立的天數。**MA 組合不得掃描。**"""
    fast = adjusted.rolling(MA_FAST).mean()
    mid = adjusted.rolling(MA_MID).mean()
    slow = adjusted.rolling(MA_SLOW).mean()
    return consecutive_true((fast > mid) & (mid > slow))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "research/results/h18_trend_stack_backward.json")
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
    forward_excess = monthly_return.sub(benchmark, axis=0)      # M13：不套未來條件

    # 形成窗內含跨停牌大跳空者排除（MA60 需要 60 個交易日）
    signal = (trend_stack_days(panel.adjusted)
              .where(panel.usable_signal_mask(MA_SLOW))
              .reindex(decisions))

    fm_panel = build_forward_panel(signal=signal, decision_frame=decision_excess,
                                   forward_frame=forward_excess,
                                   controls={"log_size": log_size},
                                   horizons=DESCRIPTIVE_HORIZONS)
    print(f"  {len(decisions)} decision months, {len(fm_panel):,} observations",
          flush=True)

    report = {
        "study": "H18 — trend-stack backward replication",
        "window": [BACKWARD_START, BACKWARD_END],
        "track": ("BACKWARD TEST on a segment with no prior outcome exposure for "
                  "this signal (research/EVIDENCE_MATRIX.md). Feature exposure "
                  "exists (signal-overlap study 2006-2014) but signal-to-signal "
                  "correlation does not reveal future returns."),
        "mandatory_disclosure": (
            "the 60-session (3-month) primary horizon was chosen by looking at the "
            "2015-2026 factor report, where stack_days IC rises monotonically with "
            "horizon (60d IC +0.0329, ICIR +0.469). That is legitimate spec "
            "selection on development data, but it must NOT be described as having "
            "been known a priori."),
        "prior_expectation": (
            "2008-2014 contains the 2008 crash (equal-weight -47.3%) and the 2009 "
            "rebound (+123.5%). Trend-persistence signals usually do worse in "
            "violently reversing regimes, so the effect is expected to be SMALLER "
            "than in the development period. A result as strong or stronger should "
            "trigger suspicion of the implementation first."),
        "signal_definition": (f"consecutive days with MA{MA_FAST} > MA{MA_MID} > "
                              f"MA{MA_SLOW} on backward-adjusted close"),
        "decision_months": int(len(decisions)),
        "observations": int(len(fm_panel)),
        "primary_trials": 1,
    }

    for spec, controls in (("size_controlled", ["log_size"]), ("raw", [])):
        cumulative = cumulative_from_path(fm_panel, factor="signal",
                                          horizons=PRIMARY_HORIZONS,
                                          lags=NEWEY_WEST_LAGS, controls=controls)
        path = response_path(fm_panel, factor="signal",
                             horizons=DESCRIPTIVE_HORIZONS,
                             lags=NEWEY_WEST_LAGS, controls=controls)
        block = {
            "primary_estimand_CUM3": {
                "horizons": list(PRIMARY_HORIZONS),
                "periods": cumulative["periods"],
                "periods_dropped": cumulative["periods_dropped"],
                "mean_pct": round(cumulative["cumulative"] * 100, 5),
                "se_pct": round(cumulative["se"] * 100, 5),
                "t_stat": round(cumulative["t_stat"], 3),
                "p_value": round(cumulative["p_value"], 4),
            },
            "descriptive_path": [
                {"horizon_months": int(r["horizon_months"]),
                 "mean_pct": round(r["mean"] * 100, 5),
                 "t_stat": round(r["t_stat"], 3)}
                for _, r in path.iterrows()
            ],
        }
        if spec == "size_controlled":
            boot = stationary_bootstrap_series_ci(cumulative["series"], mean_block=6)
            block["robustness_stationary_bootstrap"] = {
                "ci95_low_pct": round(boot["ci95_low"] * 100, 5),
                "ci95_high_pct": round(boot["ci95_high"] * 100, 5),
                "share_below_zero": round(boot["share_below_zero"], 4),
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
                "mean_without_it_pct": round(audit["mean_without_it"] * 100, 5),
            }
        report[spec] = block

    # ── 描述性：rank IC 與十分位 ────────────────────────────────
    target = forward_excess.shift(-1)
    ics, buckets = [], {d: [] for d in range(1, DECILES + 1)}
    for date in decisions:
        values = signal.loc[date].where(eligible.loc[date]).dropna()
        forward = target.loc[date]
        pair = pd.DataFrame({"s": values, "f": forward}).dropna()
        if len(pair) >= 30:
            ics.append(pair["s"].corr(pair["f"], method="spearman"))
        if len(values) >= DECILES * 5:
            rank = pd.qcut(values.rank(method="first"), DECILES, labels=False) + 1
            for decile in range(1, DECILES + 1):
                hit = forward.reindex(values.index[rank == decile]).dropna()
                if not hit.empty:
                    buckets[decile].append(float(hit.mean()))
    # **同口徑的 rank IC**：主要 horizon 是 60 個交易日（3 個月），
    # 但上面那條 rank IC 用的是次月（≈20 日）。拿 20 日的 backward 值去對照
    # development 的 60 日值是**不對等比較**，初版就這樣印過一次。
    three_month = (panel.adjusted.reindex(decisions).shift(-3)
                   / panel.adjusted.reindex(decisions) - 1.0)
    three_month_excess = three_month.sub(
        three_month.where(eligible).mean(axis=1), axis=0)
    matched = []
    for date in decisions:
        pair = pd.DataFrame({"s": signal.loc[date].where(eligible.loc[date]),
                             "f": three_month_excess.loc[date]}).dropna()
        if len(pair) >= 30:
            matched.append(pair["s"].corr(pair["f"], method="spearman"))
    matched_series = pd.Series(matched, dtype=float)
    matched_se = newey_west_se(matched_series, lags=NEWEY_WEST_LAGS)
    report["rank_ic_matched_60d"] = {
        "months": int(matched_series.size),
        "mean": round(float(matched_series.mean()), 5),
        "t_stat": round(float(matched_series.mean() / matched_se), 3)
        if matched_se and np.isfinite(matched_se) else None,
        "development_comparison_60d_ic": 0.0329,
        "note": ("this is the horizon-matched comparison. It is DESCRIPTIVE: the "
                 "pre-registered primary estimand is CUM3 via Fama-MacBeth, and a "
                 "descriptive statistic may not be promoted to the verdict."),
    }

    ic_series = pd.Series(ics, dtype=float)
    ic_se = newey_west_se(ic_series, lags=NEWEY_WEST_LAGS)
    means = {d: round(float(np.mean(v)) * 100, 4) for d, v in buckets.items() if v}
    ordered = [means[d] for d in range(1, DECILES + 1)] if len(means) == DECILES else []
    report["rank_ic"] = {
        "months": int(ic_series.size),
        "mean": round(float(ic_series.mean()), 5),
        "t_stat": round(float(ic_series.mean() / ic_se), 3)
        if ic_se and np.isfinite(ic_se) else None,
        "development_comparison_20d_ic": 0.0108,
        "horizon": "next month (~20 sessions)",
    }
    report["deciles"] = {
        "mean_pct_by_decile": means,
        "adjacent_increases_out_of_9": (
            int(sum(1 for a, b in zip(ordered, ordered[1:]) if b >= a))
            if ordered else None),
        "top_minus_bottom_pct": (round(ordered[-1] - ordered[0], 4)
                                 if ordered else None),
        "note": ("levels are not interpretable under M13 (stocks eligible at the "
                 "decision date but not the next month stay in the numerator while "
                 "leaving the benchmark's denominator); only the spread is."),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")

    c = report["size_controlled"]["primary_estimand_CUM3"]
    b = report["size_controlled"]["robustness_stationary_bootstrap"]
    lo = report["size_controlled"]["leave_one_year_out"]
    print(f"\n=== H18 主要估計量 CUM3（控制規模，2008-2014） ===")
    print(f"  CUM3 {c['mean_pct']:+.4f}%  t={c['t_stat']:+.2f}  p={c['p_value']:.4f}"
          f"  ({c['periods']} 期)")
    print(f"  bootstrap CI95 [{b['ci95_low_pct']:+.4f}%, {b['ci95_high_pct']:+.4f}%]"
          f"  逐年刪除變號 {lo['flips_sign_groups']}/{lo['years']} 組")
    print(f"  raw（不控制規模） "
          f"{report['raw']['primary_estimand_CUM3']['mean_pct']:+.4f}%  "
          f"t={report['raw']['primary_estimand_CUM3']['t_stat']:+.2f}")
    print("  路徑 " + " ".join(
        f"{p['mean_pct']:+.4f}" for p in report["size_controlled"]["descriptive_path"]))
    m = report["rank_ic_matched_60d"]
    print(f"  rank IC 20 日 {report['rank_ic']['mean']:+.5f} "
          f"(t={report['rank_ic']['t_stat']})  對照 development +0.0108")
    print(f"  rank IC 60 日 {m['mean']:+.5f} (t={m['t_stat']})"
          f"  對照 development +0.0329   ← 同口徑，但為描述性")
    print(f"  十分位遞增 {report['deciles']['adjacent_increases_out_of_9']}/9"
          f"  D10-D1 {report['deciles']['top_minus_bottom_pct']:+.4f}%")
    print(f"\nwritten: {args.output}")


if __name__ == "__main__":
    main()
