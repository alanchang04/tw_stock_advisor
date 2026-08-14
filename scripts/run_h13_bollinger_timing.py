"""H13：BB／量能作為進場時機（登記見 `research/HYPOTHESES.md` H13）。

**問題**：既然已經決定要買 Revenue+ 的股票，等待價格與量能進入擴張狀態
（B1）再進場，能不能更有效率地建立部位？

比較兩個**當下可執行**的 policy，從同一批 Revenue+ 候選出發：

    F      營收條件成立 → T+1 開盤買
    F+BB   營收條件成立 → 最多等 W = 20 個交易日；
           期間出現 B1 就在其 T+1 開盤買；20 日內未出現則**不買**

## 三個會讓結論走樣的地方

**M16 未來事件條件化。** 不得用「後來有沒有出現 B1」篩樣本——那與先前把
布林效果放大 8 倍的「只取後來有 B2 的 B1」同一類錯誤。
`research.policy:wait_for_trigger_entry` 會把**沒等到的候選保留在結果中**
（`entered=False`），使它們不可能從樣本消失。

**M17 曝險幻覺。** 等越久、跳過越多股票，MAE 天生越好看；
**「永遠不買」的 MAE 是完美的 0%。** 因此 participation、等待天數、
放棄的報酬、最終報酬全部必報，且報酬要分兩種口徑：

- **每個已進場部位**的報酬（條件於有進場）
- **每個候選**的報酬（沒進場＝現金＝0）——這一項才看得出「少買」的代價

**H01 只測 return，那對 timing 訊號是錯的指標。** 主要指標改為
median MAE、Q25(MAE)、time-to-breakeven；return 並列但不單獨作判準。

結論效力：2015~2026 對本假說**只能否證**（BB B1 已被 H01 在同一段資料上
測過、修正過、看過分布）。confirmatory 只能來自 forward。

試驗次數：5，事前登記。
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

from agent.strategy import apply_total_return_adjustment, split_adjust  # noqa: E402
from research.bollinger import bollinger_bands, setup_conditions  # noqa: E402
from research.fama_macbeth import newey_west_se  # noqa: E402
from research.inference import ROUND_TRIP_COST  # noqa: E402
from research.policy import (  # noqa: E402
    exposure_summary, max_adverse_excursion, restricted_mean_time,
    time_to_breakeven, wait_for_trigger_entry,
)
import scripts.run_h04_h06_event_study as H04  # noqa: E402
import scripts.run_h12_institutional_confirmation as H12  # noqa: E402

MAX_WAIT_SESSIONS = 20        # 事前固定，policy 的一部分
PRIMARY_HORIZON = 60          # 交易日；依 M12
REVENUE_MIN_YOY = 0.0         # 主要規格，與 H12／H16 一致
DOWNSIDE_QUANTILE = 0.25      # 事前只鎖 Q25，不掃 q10/q20/q33/q50
NEWEY_WEST_LAGS = 6


def first_breakout_frame(closes: pd.DataFrame, volume: pd.DataFrame) -> pd.DataFrame:
    """B1：帶寬擴張 ＋ 量能達標 ＋ 收盤**首次**突破上沿。

    與 H01／H10 的 `first_breaks` 同一條規則，逐檔算完後拼成寬表，
    供 `wait_for_trigger_entry` 使用。
    """
    columns = {}
    for stock_id in closes.columns:
        close = closes[stock_id].dropna()
        if len(close) < 80:
            continue
        _, upper, _ = bollinger_bands(close)
        expanded, volume_ok = setup_conditions(close, volume[stock_id].reindex(close.index))
        outside = (close > upper).fillna(False)
        fresh = outside & ~outside.shift(1, fill_value=False)
        columns[stock_id] = (fresh & expanded & volume_ok).reindex(closes.index).fillna(False)
    return pd.DataFrame(columns, index=closes.index).fillna(False).astype(bool)


def policy_outcomes(entries: pd.DataFrame, *, opens, lows, closes, adjusted,
                    sessions, horizon: int) -> pd.DataFrame:
    """對已進場的列計算 MAE、time-to-breakeven 與 horizon 報酬。"""
    taken = entries[entries["entered"]].copy()
    if taken.empty:
        return taken.assign(mae=np.nan, duration=np.nan, event=np.nan, ret=np.nan)
    taken["mae"] = max_adverse_excursion(
        taken, opens, lows, sessions=sessions, horizon=horizon)
    timing = time_to_breakeven(taken, opens, closes, sessions=sessions,
                               horizon=horizon, threshold=ROUND_TRIP_COST)
    taken["duration"] = timing["duration"]
    taken["event"] = timing["event"]

    calendar = pd.Series(range(len(sessions)), index=sessions)
    returns = []
    for stock_id, entry in zip(taken["stock_id"], taken["entry_date"]):
        position = calendar.get(entry, None)
        if position is None or stock_id not in adjusted.columns:
            returns.append(np.nan)
            continue
        exit_position = int(position) + horizon
        if exit_position >= len(sessions):
            returns.append(np.nan)
            continue
        start = adjusted.iloc[int(position)][stock_id]
        end = adjusted.iloc[exit_position][stock_id]
        returns.append(float(end / start - 1.0)
                       if np.isfinite(start) and start > 0 and np.isfinite(end) else np.nan)
    taken["ret"] = returns
    return taken


def monthly_stat(values_by_month: list, *, pct: bool = True) -> dict:
    """逐月統計再對月取平均（等權月份），並以 Newey-West 給 t。"""
    series = pd.Series(values_by_month, dtype=float).dropna()
    if series.empty:
        return {"months": 0}
    se = newey_west_se(series, lags=NEWEY_WEST_LAGS) if len(series) > 1 else np.nan
    scale = 100.0 if pct else 1.0
    return {
        "months": int(series.size),
        "mean": round(float(series.mean()) * scale, 4),
        "t_stat": round(float(series.mean() / se), 3)
        if se and np.isfinite(se) else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "research/results/h13_bollinger_timing.json")
    args = parser.parse_args()

    prices = pd.read_parquet(
        args.snapshot / "prices.parquet",
        columns=["stock_id", "trade_date", "open", "low", "close", "volume", "turnover"])
    prices["stock_id"] = prices["stock_id"].astype(str)
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    wide = {n: prices.pivot(index="trade_date", columns="stock_id", values=n).sort_index()
            for n in ("open", "low", "close", "volume", "turnover")}

    dividends = pd.read_parquet(args.snapshot / "dividend_events.parquet")
    dividends["stock_id"] = dividends["stock_id"].astype(str)
    dividends["ex_date"] = pd.to_datetime(dividends["ex_date"])
    adjusted = apply_total_return_adjustment(wide["close"].apply(split_adjust), dividends)

    sessions = pd.DatetimeIndex(wide["close"].index)
    mask = H04.liquid_universe_mask(wide["close"], wide["turnover"])
    print("building B1 trigger frame ...", flush=True)
    trigger = first_breakout_frame(wide["close"], wide["volume"])

    H12.revenue_positive.cache = H12.load_revenue(args.snapshot)
    last_usable = sessions[-(PRIMARY_HORIZON + MAX_WAIT_SESSIONS + 3)]
    decisions = H12.monthly_decision_dates(sessions, sessions[0], last_usable)

    per_month = {key: [] for key in
                 ("f_mae", "bb_mae", "f_q25", "bb_q25", "f_rmt", "bb_rmt",
                  "f_ret", "bb_ret", "f_policy_ret", "bb_policy_ret",
                  "missed", "participation", "wait")}
    all_entries = {"F": [], "F+BB": []}

    for decision in decisions:
        eligible = [s for s in wide["close"].columns if bool(mask.at[decision, s])]
        winners = sorted(H12.revenue_positive(args.snapshot, decision, REVENUE_MIN_YOY)
                         & set(eligible))
        if len(winners) < 30:
            continue
        candidates = pd.DataFrame({"stock_id": winners, "decision_date": decision})

        position = sessions.get_loc(decision)
        if position + 1 >= len(sessions):
            continue
        # Policy F：決策日次一交易日開盤直接買，全部候選都進場
        baseline = candidates.assign(entry_date=sessions[position + 1],
                                     wait_sessions=0.0, entered=True)
        # Policy F+BB：最多等 20 日；沒等到就不買（未進場者保留在結果中）
        timed = wait_for_trigger_entry(candidates, trigger, sessions=sessions,
                                       max_wait_sessions=MAX_WAIT_SESSIONS)

        outcomes = {}
        for label, frame in (("F", baseline), ("F+BB", timed)):
            outcomes[label] = policy_outcomes(
                frame, opens=wide["open"], lows=wide["low"], closes=wide["close"],
                adjusted=adjusted, sessions=sessions, horizon=PRIMARY_HORIZON)
            all_entries[label].append(outcomes[label].assign(decision=decision))

        f, bb = outcomes["F"], outcomes["F+BB"]
        if f.empty or bb.empty:
            continue
        per_month["f_mae"].append(f["mae"].median())
        per_month["bb_mae"].append(bb["mae"].median())
        per_month["f_q25"].append(f["mae"].quantile(DOWNSIDE_QUANTILE))
        per_month["bb_q25"].append(bb["mae"].quantile(DOWNSIDE_QUANTILE))
        per_month["f_rmt"].append(restricted_mean_time(f, horizon=PRIMARY_HORIZON))
        per_month["bb_rmt"].append(restricted_mean_time(bb, horizon=PRIMARY_HORIZON))
        per_month["f_ret"].append(f["ret"].mean())
        per_month["bb_ret"].append(bb["ret"].mean())
        # policy 口徑：沒進場＝現金＝0，這一項才看得出「少買」的代價
        per_month["f_policy_ret"].append(f["ret"].fillna(0.0).sum() / len(candidates))
        per_month["bb_policy_ret"].append(bb["ret"].fillna(0.0).sum() / len(candidates))
        summary = exposure_summary(timed)
        per_month["participation"].append(summary["participation_rate"])
        per_month["wait"].append(summary["mean_wait_sessions"])

        # 等待期間放棄的報酬：從 F 的進場日到 BB 的實際進場日
        entered = bb[bb["entered"]]
        if len(entered):
            gaps = []
            calendar = pd.Series(range(len(sessions)), index=sessions)
            for stock_id, entry in zip(entered["stock_id"], entered["entry_date"]):
                start = int(calendar[sessions[position + 1]])
                stop = calendar.get(entry, None)
                if stop is None or stock_id not in adjusted.columns:
                    continue
                a, b = adjusted.iloc[start][stock_id], adjusted.iloc[int(stop)][stock_id]
                if np.isfinite(a) and a > 0 and np.isfinite(b):
                    gaps.append(float(b / a - 1.0))
            per_month["missed"].append(float(np.mean(gaps)) if gaps else np.nan)

    def paired(a_key: str, b_key: str) -> dict:
        a = pd.Series(per_month[a_key], dtype=float)
        b = pd.Series(per_month[b_key], dtype=float)
        return monthly_stat((b - a).tolist())

    report = {
        "study": "H13 Bollinger/volume as entry timing within Revenue+",
        "registration": "research/HYPOTHESES.md H13",
        "policies": {
            "F": "Revenue+ -> buy at next session open",
            "F+BB": (f"Revenue+ -> wait up to {MAX_WAIT_SESSIONS} sessions for a B1; "
                     "buy at the session after the breakout; if none, do not buy"),
        },
        "primary_horizon_sessions": PRIMARY_HORIZON,
        "evidence_status": {
            "historical": "falsification / development evidence ONLY",
            "confirmatory": "forward period after 2026-08-14 only",
            "reason": ("the B1 definition was already tested, corrected and had its "
                       "return distribution inspected on 2015-2026 under H01"),
        },
        "trials_registered": 5,
        "months": len(per_month["f_mae"]),
        "primary_outcomes": {
            "median_mae": {"F": monthly_stat(per_month["f_mae"]),
                           "F+BB": monthly_stat(per_month["bb_mae"]),
                           "difference_bb_minus_f": paired("f_mae", "bb_mae")},
            "q25_mae": {"F": monthly_stat(per_month["f_q25"]),
                        "F+BB": monthly_stat(per_month["bb_q25"]),
                        "difference_bb_minus_f": paired("f_q25", "bb_q25")},
            "restricted_mean_time_to_breakeven_sessions": {
                "F": monthly_stat(per_month["f_rmt"], pct=False),
                "F+BB": monthly_stat(per_month["bb_rmt"], pct=False),
                "difference_bb_minus_f": monthly_stat(
                    (pd.Series(per_month["bb_rmt"]) - pd.Series(per_month["f_rmt"])).tolist(),
                    pct=False)},
        },
        "returns": {
            "per_entered_position": {"F": monthly_stat(per_month["f_ret"]),
                                     "F+BB": monthly_stat(per_month["bb_ret"]),
                                     "difference_bb_minus_f": paired("f_ret", "bb_ret")},
            "per_candidate_cash_if_not_entered": {
                "F": monthly_stat(per_month["f_policy_ret"]),
                "F+BB": monthly_stat(per_month["bb_policy_ret"]),
                "difference_bb_minus_f": paired("f_policy_ret", "bb_policy_ret")},
        },
        "exposure": {
            "participation_rate": monthly_stat(per_month["participation"], pct=False),
            "mean_wait_sessions": monthly_stat(per_month["wait"], pct=False),
            "missed_return_while_waiting": monthly_stat(per_month["missed"]),
        },
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                      default=str) + "\n", encoding="utf-8")
    print(f"\nwritten: {args.output}")


if __name__ == "__main__":
    main()
