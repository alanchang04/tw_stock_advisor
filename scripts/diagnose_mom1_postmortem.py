"""MOM-1 事後解剖四項（**EXPLORATORY — CANNOT REVERSE F1**）。

MOM-1 已於 2026-08-14 依 SPEC §9.5 否決，判準在開封前凍結。
本檔的四項診斷**只用於歸因與決定「動能這條線還值不值得存在」**，
依 §8.3 全部標示 exploratory，不得用於翻案、不得改任何參數。

四項各自堵住 F1 報告的一個推論漏洞：

A. **配對 bootstrap**。原報告 bootstrap 的是「策略 CAGR 的 CI 含不含 0」，
   但那不是真正的虛無假設。策略與市場高度同漲跌，單獨看策略報酬的 CI 會非常寬；
   真正該問的是 `R_MOM − R_benchmark`，配對之後精度高得多。

B. **等權 universe + MA200 基準**。MOM-1B 的 beta 只有 0.24、平均現金 41.7%、
   703 個交易日完全空手——它已經有一大部分是 market timing 策略。
   拿它去比「全天候等權 universe」有歸因問題：分不出是動能選股有價值，
   還是單純 MA200 擇時有價值。

C. **P90 dummy ＋ 規模控制**。D10 的 +0.35%／月可能只是小型股曝險。
   月營收研究已經吃過這個虧，動能也該做一次同樣的控制。

D. **T+1 開盤起算的訊號報酬**。原診斷用「月末收盤 → 下月末收盤」，
   那對 L1 不構成前視（確實是未來），但策略實際是 T+1 開盤成交。
   若隔夜跳空吃掉一部分，那部分是「可預測但賺不到」。
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

from research.fama_macbeth import newey_west_se  # noqa: E402
from research.momentum import (  # noqa: E402
    compute_mom_6_1, eligible_universe, month_end_sessions, next_session,
    pit_common_stock_mask,
)
from research.momentum_backtest import (  # noqa: E402
    _market_filter_risk_off, benchmark_nav, simulate,
)
from research.momentum_release import load_momentum_release  # noqa: E402
from research.performance import core_metrics, monthly_returns  # noqa: E402

RELEASE_ID = "tw_stock_data_2005_2014_r3"
START, END = "2008-01-01", "2014-12-31"
LEDGER_PATH = "reports/twse_execution_action_ledger_2005_2014.csv"
BLOCK_MONTHS = 6            # 與 §8.3 同一個事前固定值
DRAWS = 1000
TOP_DECILE = 0.90           # P90 門檻，對應 MOM-1 的「前 10% 進場」


def decision_dates(sessions: pd.DatetimeIndex) -> list:
    return [d for d in month_end_sessions(sessions)
            if pd.Timestamp(START) <= d <= pd.Timestamp(END)]


def eligible_at(inputs, signal, as_of):
    pit_mask = pit_common_stock_mask(inputs.pit_master, as_of,
                                     inputs.adjusted_close.columns)
    return eligible_universe(
        as_of=as_of, adjusted_close=inputs.adjusted_close,
        raw_close=inputs.raw_close, turnover=inputs.turnover, signal=signal,
        pit_mask=pit_mask, restricted=inputs.restricted)


# ── A. 配對 block bootstrap ────────────────────────────────────────────
def paired_bootstrap(strategy: pd.Series, benchmark: pd.Series, *,
                     seed: int = 20260814) -> dict:
    """對「策略月報酬 − 基準月報酬」做 block bootstrap（§8.3 同一 block 長度）。

    配對之後市場共同成分被消掉，剩下的才是真正想估的相對 edge。
    """
    pair = pd.concat([monthly_returns(strategy), monthly_returns(benchmark)],
                     axis=1, keys=["strategy", "benchmark"]).dropna()
    difference = (pair["strategy"] - pair["benchmark"]).to_numpy(dtype=float)
    if len(difference) < BLOCK_MONTHS * 2:
        return {"months": int(len(difference))}
    blocks = [difference[i:i + BLOCK_MONTHS]
              for i in range(0, len(difference) - BLOCK_MONTHS + 1)]
    rng = np.random.default_rng(seed)
    needed = int(np.ceil(len(difference) / BLOCK_MONTHS))
    means = np.empty(DRAWS)
    for i in range(DRAWS):
        picked = rng.integers(0, len(blocks), size=needed)
        means[i] = np.concatenate([blocks[j] for j in picked])[:len(difference)].mean()
    observed = float(difference.mean())
    return {
        "months": int(len(difference)),
        "mean_monthly_excess_pct": round(observed * 100, 4),
        "annualised_excess_pct": round(((1 + observed) ** 12 - 1) * 100, 4),
        "ci95_monthly_pct": [round(float(np.percentile(means, 2.5)) * 100, 4),
                             round(float(np.percentile(means, 97.5)) * 100, 4)],
        "p_value_one_sided_le_zero": round(float((means <= 0).mean()), 4),
    }


# ── B. 等權 universe（可選 MA200 擇時）────────────────────────────────
def equal_weight_nav(inputs, signal, *, apply_filter: bool,
                     capital: float = 300_000.0) -> pd.Series:
    """等權持有當期合格 universe；``apply_filter`` 時套用與 MOM-1B 相同的 MA200 規則。

    這條基準把「擇時」與「選股」分開：
    `MOM-1B − (等權 + MA200)` 才是控制擇時之後、動能選股自己的貢獻。
    """
    sessions = pd.DatetimeIndex(inputs.trading_days)
    dates = decision_dates(sessions)
    risk_off = _market_filter_risk_off(inputs.adjusted_close)
    adjusted = inputs.adjusted_close
    level, points = capital, [(dates[0], capital)]
    for current, following in zip(dates, dates[1:]):
        # 與 MOM-1B 同一個資訊集：決策日收盤看濾網，下一期是否持有由此決定
        if apply_filter and bool(risk_off.get(current, False)):
            points.append((following, level))
            continue
        eligible = eligible_at(inputs, signal, current)
        if len(eligible):
            step = (adjusted.loc[following, eligible]
                    / adjusted.loc[current, eligible] - 1.0).dropna()
            level *= (1.0 + float(step.mean())) if len(step) else 1.0
        points.append((following, level))
    return pd.Series(dict(points)).sort_index()


# ── C. P90 dummy ＋ 規模控制 ──────────────────────────────────────────
def pit_market_cap(market_structure: pd.DataFrame, as_of, stock_ids) -> pd.Series:
    """以 as_of 當日之前最新的官方快照計算市值（issued_shares × close）。"""
    frame = market_structure.copy()
    frame["snapshot_date"] = pd.to_datetime(frame["snapshot_date"], errors="coerce")
    frame["stock_id"] = frame["stock_id"].astype(str)
    past = frame[frame["snapshot_date"] <= pd.Timestamp(as_of)]
    ids = pd.Index([str(s) for s in stock_ids])
    if past.empty:
        return pd.Series(np.nan, index=ids, dtype=float)
    latest = past[past["snapshot_date"] == past["snapshot_date"].max()]
    latest = latest.drop_duplicates("stock_id").set_index("stock_id")
    shares = pd.to_numeric(latest.get("issued_shares"), errors="coerce")
    close = pd.to_numeric(latest.get("close"), errors="coerce")
    return (shares * close).reindex(ids).astype(float)


def top_decile_with_size_control(inputs, signal) -> dict:
    """逐月 `R[i,t+1] = a + b·I(mom > P90) + c·log(市值) + e`，再對 b 做 Newey-West。

    b 是「控制規模之後，最右尾相對其他合格股票多出多少」。
    若加了規模控制 b 就塌掉，代表 D10 的優勢主要是小型股曝險而不是動能。
    """
    sessions = pd.DatetimeIndex(inputs.trading_days)
    dates = decision_dates(sessions)
    adjusted = inputs.adjusted_close
    raw, controlled, widths = [], [], []
    for current, following in zip(dates, dates[1:]):
        eligible = eligible_at(inputs, signal, current)
        if len(eligible) < 30:
            continue
        values = signal.loc[current, eligible].dropna()
        forward = (adjusted.loc[following, values.index]
                   / adjusted.loc[current, values.index] - 1.0)
        cap = pit_market_cap(inputs.market_structure, current, values.index)
        frame = pd.concat([values, forward, np.log(cap.where(cap > 0))],
                          axis=1, keys=["signal", "forward", "log_cap"]).dropna()
        if len(frame) < 30:
            continue
        flag = (frame["signal"] > frame["signal"].quantile(TOP_DECILE)).astype(float)
        if flag.nunique() < 2:
            continue
        y = frame["forward"].to_numpy(dtype=float)
        raw.append(float(np.linalg.lstsq(
            np.column_stack([np.ones(len(flag)), flag.to_numpy()]), y, rcond=None)[0][1]))
        design = np.column_stack([np.ones(len(flag)), flag.to_numpy(),
                                  frame["log_cap"].to_numpy(dtype=float)])
        controlled.append(float(np.linalg.lstsq(design, y, rcond=None)[0][1]))
        widths.append(len(frame))

    def summarise(values: list) -> dict:
        series = pd.Series(values, dtype=float).dropna()
        if series.empty:
            return {"months": 0}
        se = newey_west_se(series, lags=6)
        return {"months": int(series.size),
                "mean_monthly_pct": round(float(series.mean()) * 100, 4),
                "t_stat": round(float(series.mean() / se), 3)
                if se and np.isfinite(se) else None}

    return {"median_cross_section": int(np.median(widths)) if widths else 0,
            "raw": summarise(raw), "size_controlled": summarise(controlled)}


# ── D. T+1 開盤起算的訊號報酬 ─────────────────────────────────────────
def executable_signal_returns(inputs, signal) -> dict:
    """把「可預測」與「賺得到」分開：報酬改由 T+1 開盤算到下期 T+1 開盤。

    月末收盤 → 下月末收盤 對 L1 沒有前視（確實是未來），
    但策略實際在 T+1 開盤成交；若隔夜跳空吃掉一部分，那部分預測得到卻賺不到。
    """
    sessions = pd.DatetimeIndex(inputs.trading_days)
    dates = decision_dates(sessions)
    opens = inputs.raw_open
    ics, spreads = [], []
    for current, following in zip(dates, dates[1:]):
        entry, exit_ = next_session(sessions, current), next_session(sessions, following)
        if entry is None or exit_ is None:
            continue
        eligible = eligible_at(inputs, signal, current)
        if len(eligible) < 30:
            continue
        values = signal.loc[current, eligible].dropna()
        forward = (opens.loc[exit_, values.index] / opens.loc[entry, values.index] - 1.0)
        pair = pd.concat([values, forward], axis=1,
                         keys=["signal", "forward"]).dropna()
        if len(pair) < 30:
            continue
        ics.append(float(pair["signal"].corr(pair["forward"], method="spearman")))
        top = pair["forward"][pair["signal"] > pair["signal"].quantile(TOP_DECILE)]
        spreads.append(float(top.mean() - pair["forward"].mean()))

    out = {}
    for name, values in (("rank_ic", ics), ("top_decile_minus_universe", spreads)):
        series = pd.Series(values, dtype=float).dropna()
        se = newey_west_se(series, lags=6) if len(series) > 1 else float("nan")
        out[name] = {
            "months": int(series.size),
            "mean": round(float(series.mean()), 5) if name == "rank_ic"
            else round(float(series.mean()) * 100, 4),
            "t_stat": round(float(series.mean() / se), 3)
            if se and np.isfinite(se) else None,
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "reports/mom1_postmortem.json")
    args = parser.parse_args()

    inputs = load_momentum_release(RELEASE_ID, root=ROOT)
    signal = compute_mom_6_1(inputs.adjusted_close)
    ledger = pd.read_csv(ROOT / LEDGER_PATH)
    ledger["event_date"] = pd.to_datetime(ledger["event_date"], errors="coerce")
    ledger = ledger[ledger["event_date"].notna()]

    print("B. 等權基準（含／不含 MA200）...", flush=True)
    equal_weight = equal_weight_nav(inputs, signal, apply_filter=False)
    equal_weight_timed = equal_weight_nav(inputs, signal, apply_filter=True)
    zero_fifty = benchmark_nav(inputs, start=START, end=END)

    variants = {}
    for variant in ("MOM-1A", "MOM-1B"):
        print(f"A. {variant} 配對 bootstrap ...", flush=True)
        result = simulate(inputs, variant=variant, signal=signal, ledger=ledger,
                          start=START, end=END)
        reference = equal_weight_timed if variant == "MOM-1B" else equal_weight
        variants[variant] = {
            "vs_0050": paired_bootstrap(result.nav, zero_fifty),
            "vs_equal_weight_universe": paired_bootstrap(result.nav, equal_weight),
            "vs_attribution_baseline": {
                "baseline": ("equal weight + MA200" if variant == "MOM-1B"
                             else "equal weight (no timing)"),
                **paired_bootstrap(result.nav, reference),
            },
        }

    print("C. P90 dummy + 規模控制 ...", flush=True)
    size = top_decile_with_size_control(inputs, signal)
    print("D. T+1 開盤起算 ...", flush=True)
    executable = executable_signal_returns(inputs, signal)

    report = {
        "study": "MOM-1 post-mortem diagnostics",
        "status": "EXPLORATORY — CANNOT REVERSE F1",
        "note": ("MOM-1 已依 SPEC 9.5 否決，判準開封前凍結。本檔僅供歸因與"
                 "『動能這條線還值不值得存在』的判斷，不得用於翻案（SPEC 8.3）。"),
        "data_release_id": RELEASE_ID,
        "window": {"start": START, "end": END},
        "block_months": BLOCK_MONTHS,
        "bootstrap_draws": DRAWS,
        "B_baselines": {
            "0050_total_return": {
                "final_nav": round(float(zero_fifty.iloc[-1]), 2),
                "cagr": round(core_metrics(zero_fifty)["cagr"], 4)},
            "equal_weight_universe": {
                "final_nav": round(float(equal_weight.iloc[-1]), 2),
                "total_return": round(float(equal_weight.iloc[-1]
                                            / equal_weight.iloc[0] - 1), 4)},
            "equal_weight_plus_ma200": {
                "final_nav": round(float(equal_weight_timed.iloc[-1]), 2),
                "total_return": round(float(equal_weight_timed.iloc[-1]
                                            / equal_weight_timed.iloc[0] - 1), 4)},
        },
        "A_paired_bootstrap": variants,
        "C_top_decile_size_control": size,
        "D_executable_from_next_open": executable,
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                      default=str) + "\n", encoding="utf-8")
    print(f"\nwritten: {args.output}")


if __name__ == "__main__":
    main()
