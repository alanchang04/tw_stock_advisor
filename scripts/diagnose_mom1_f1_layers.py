"""MOM-1 F1 的分層拆解與數字驗算（**exploratory 診斷，不得用於翻案**）。

依 SPEC §8.3「所有未預先登記的切片只能標示 exploratory，不得決定採用」，
本腳本的一切輸出都是**事後診斷**：MOM-1 已於 2026-08-14 依 §9.5 否決，
這裡只回答「為什麼失敗、失敗在哪一層」，不新增策略變體、不改任何參數。

拆成三層問不同的問題：

    L1  台股 2008~2014 有沒有中期動能現象？        → 訊號分位的前瞻報酬
    L2  mom_6_1 這個定義能不能捕捉它？             → 逐月 rank IC
    L3  「前10%/後20%/10檔等權/產業上限」能不能    → 組合 gross 與 net
        把它變成可執行策略？

L1/L2 成立而 L3 失敗，代表 **MOM-1 這個策略失敗，但動能研究線仍活著**；
三層都失敗才是「台股沒有這個現象」。

同時做四項對帳，確認正式報告的數字沒有算錯。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.strategy import FEE_RATE, TAX_RATE  # noqa: E402
from research.fama_macbeth import newey_west_se  # noqa: E402
from research.momentum import (  # noqa: E402
    compute_mom_6_1, eligible_universe, month_end_sessions, pit_common_stock_mask,
)
from research.momentum_backtest import benchmark_nav, simulate  # noqa: E402
from research.momentum_release import load_momentum_release  # noqa: E402
from research.performance import core_metrics, monthly_returns  # noqa: E402

RELEASE_ID = "tw_stock_data_2005_2014_r3"
START, END = "2008-01-01", "2014-12-31"
LEDGER_PATH = "reports/twse_execution_action_ledger_2005_2014.csv"
DECILES = 10


def signal_layers(inputs, signal: pd.DataFrame) -> dict:
    """L1／L2：訊號分位的前瞻報酬與逐月 rank IC。

    **完全不經過組合與執行**：前瞻報酬直接用還原收盤價從本次決策日算到下次決策日，
    沒有成本、沒有 T+1、沒有 10 檔上限。這樣才能把「現象存不存在」
    與「組合建構有沒有保留住它」分開。
    """
    sessions = pd.DatetimeIndex(inputs.trading_days)
    decisions = [d for d in month_end_sessions(sessions)
                 if pd.Timestamp(START) <= d <= pd.Timestamp(END)]
    adjusted = inputs.adjusted_close

    per_decile: dict[int, list[float]] = {d: [] for d in range(1, DECILES + 1)}
    ics, spreads, widths = [], [], []
    for current, following in zip(decisions, decisions[1:]):
        pit_mask = pit_common_stock_mask(inputs.pit_master, current, adjusted.columns)
        eligible = eligible_universe(
            as_of=current, adjusted_close=adjusted, raw_close=inputs.raw_close,
            turnover=inputs.turnover, signal=signal, pit_mask=pit_mask,
            restricted=inputs.restricted)
        if len(eligible) < DECILES * 3:
            continue
        values = signal.loc[current, eligible].dropna()
        forward = (adjusted.loc[following, values.index]
                   / adjusted.loc[current, values.index] - 1.0)
        pair = pd.concat([values, forward], axis=1, keys=["signal", "forward"]).dropna()
        if len(pair) < DECILES * 3:
            continue

        bucket = pd.qcut(pair["signal"].rank(method="first"), DECILES,
                         labels=range(1, DECILES + 1))
        grouped = pair.groupby(bucket, observed=True)["forward"].mean()
        for decile, value in grouped.items():
            per_decile[int(decile)].append(float(value))
        spreads.append(float(grouped.get(DECILES, np.nan) - grouped.get(1, np.nan)))
        ics.append(float(pair["signal"].corr(pair["forward"], method="spearman")))
        widths.append(int(len(pair)))

    ic_series = pd.Series(ics, dtype=float).dropna()
    ic_se = newey_west_se(ic_series, lags=6)
    spread_series = pd.Series(spreads, dtype=float).dropna()
    spread_se = newey_west_se(spread_series, lags=6)
    return {
        "months": int(len(ic_series)),
        "median_cross_section": int(np.median(widths)) if widths else 0,
        "decile_mean_monthly_return_pct": {
            str(decile): round(float(np.mean(values)) * 100, 4)
            for decile, values in per_decile.items() if values
        },
        "top_minus_bottom_monthly_pct": round(float(spread_series.mean()) * 100, 4),
        "top_minus_bottom_t_stat": round(float(spread_series.mean() / spread_se), 3)
        if spread_se and np.isfinite(spread_se) else None,
        "mean_rank_ic": round(float(ic_series.mean()), 5),
        "rank_ic_t_stat": round(float(ic_series.mean() / ic_se), 3)
        if ic_se and np.isfinite(ic_se) else None,
        "rank_ic_positive_month_share": round(float((ic_series > 0).mean()), 4),
    }


def equal_weight_universe_nav(inputs, signal: pd.DataFrame,
                              capital: float = 300_000.0) -> pd.Series:
    """SPEC §8.1 基準 2：MOM universe 等權含息組合。

    **這是把「動能 edge」與「等權／小型股傾斜」分開的關鍵基準。**
    0050 是市值加權且高度集中於少數大型股；MOM-1 在較寬的普通股母體裡等權買 10 檔。
    若 gross MOM-1 只贏 0050 而贏不了等權 universe，那它賺到的就不是動能，
    而是等權本身的傾斜——先前 H01 研究已經被這件事騙過一次。

    每個月末以當時的合格 universe 等權持有到下一個決策日，逐月連乘。不計成本。
    """
    sessions = pd.DatetimeIndex(inputs.trading_days)
    decisions = [d for d in month_end_sessions(sessions)
                 if pd.Timestamp(START) <= d <= pd.Timestamp(END)]
    adjusted = inputs.adjusted_close
    level, points = capital, [(decisions[0], capital)]
    for current, following in zip(decisions, decisions[1:]):
        pit_mask = pit_common_stock_mask(inputs.pit_master, current, adjusted.columns)
        eligible = eligible_universe(
            as_of=current, adjusted_close=adjusted, raw_close=inputs.raw_close,
            turnover=inputs.turnover, signal=signal, pit_mask=pit_mask,
            restricted=inputs.restricted)
        if len(eligible) == 0:
            points.append((following, level))
            continue
        step = (adjusted.loc[following, eligible]
                / adjusted.loc[current, eligible] - 1.0).dropna()
        level *= (1.0 + float(step.mean())) if len(step) else 1.0
        points.append((following, level))
    return pd.Series(dict(points)).sort_index()


def run_variant(inputs, signal, ledger, variant: str, *, gross: bool):
    """跑一個變體。``gross=True`` 時把手續費／證交稅／滑價全部歸零。

    歸零是為了回答「組合建構有沒有保留住訊號」，
    **不是**為了讓策略看起來比較好——net 才是判決依據，gross 只作歸因。
    """
    if not gross:
        return simulate(inputs, variant=variant, signal=signal, ledger=ledger,
                        start=START, end=END)
    identity = lambda price: float(price)   # noqa: E731 - 無滑價的成交價
    with patch("research.momentum_execution.FEE_RATE", 0.0), \
         patch("research.momentum_execution.TAX_RATE", 0.0), \
         patch("research.momentum_execution.buy_fill", identity), \
         patch("research.momentum_execution.sell_fill", identity), \
         patch("research.momentum_backtest.buy_fill", identity):
        return simulate(inputs, variant=variant, signal=signal, ledger=ledger,
                        start=START, end=END)


def reconcile(result, capital: float) -> dict:
    """對帳：期末淨值變化應等於「已實現損益 + 未實現損益」。

    這條對帳會抓到部位帳漏記現金流的錯誤——例如公司行動配息沒進現金、
    或賣出淨額算錯。兩邊對不上就代表帳有洞。
    """
    realised = float(result.trades["net_pnl"].sum()) if len(result.trades) else 0.0
    open_positions = result.diagnostics["open_positions_at_end"]
    unrealised = float(sum(p["unrealised_pnl"] for p in open_positions))
    nav_change = float(result.nav.iloc[-1] - capital)
    return {
        "nav_change": round(nav_change, 2),
        "realised_pnl": round(realised, 2),
        "unrealised_pnl_open_positions": round(unrealised, 2),
        "sum": round(realised + unrealised, 2),
        "residual": round(nav_change - realised - unrealised, 2),
        "reconciled": bool(abs(nav_change - realised - unrealised) < 1.0),
    }


def distribution(result) -> dict:
    """L5：報酬集中在哪裡——年度、個股、以及拿掉最佳年份之後。"""
    trades = result.trades
    monthly = monthly_returns(result.nav)
    annual = {}
    for year, group in monthly.groupby(monthly.index.year):
        annual[str(year)] = float(np.prod(1.0 + group.to_numpy()) - 1.0)
    best_year = max(annual, key=annual.get) if annual else None
    without_best = float(np.prod([1.0 + v for y, v in annual.items()
                                  if y != best_year]) - 1.0) if annual else float("nan")

    by_stock = {}
    if len(trades):
        by_stock = (trades.groupby("stock_id")["net_pnl"].sum()
                    .sort_values(ascending=False))
    gross_profit = float(by_stock[by_stock > 0].sum()) if len(by_stock) else 0.0
    return {
        "annual_returns": {k: round(v, 4) for k, v in sorted(annual.items())},
        "best_year": best_year,
        "compound_return_excluding_best_year": round(without_best, 4),
        "distinct_stocks_traded": int(by_stock.size) if len(by_stock) else 0,
        "top3_stocks_share_of_gross_profit": (
            round(float(by_stock.head(3).sum() / gross_profit), 4)
            if gross_profit > 0 else None),
        "top3_stocks": (
            {str(k): round(float(v)) for k, v in by_stock.head(3).items()}
            if len(by_stock) else {}),
        "worst3_stocks": (
            {str(k): round(float(v)) for k, v in by_stock.tail(3).items()}
            if len(by_stock) else {}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "reports/mom1_f1_layer_diagnostics.json")
    args = parser.parse_args()

    inputs = load_momentum_release(RELEASE_ID, root=ROOT)
    signal = compute_mom_6_1(inputs.adjusted_close)
    ledger = pd.read_csv(ROOT / LEDGER_PATH)
    ledger["event_date"] = pd.to_datetime(ledger["event_date"], errors="coerce")
    ledger = ledger[ledger["event_date"].notna()]

    print("L1/L2 訊號層 ...", flush=True)
    layers = signal_layers(inputs, signal)

    benchmark = benchmark_nav(inputs, start=START, end=END)
    print("等權 universe 基準 ...", flush=True)
    equal_weight = equal_weight_universe_nav(inputs, signal)
    capital = 300_000.0

    variants: dict = {}
    for variant in ("MOM-1A", "MOM-1B"):
        print(f"L3 {variant} net ...", flush=True)
        net = run_variant(inputs, signal, ledger, variant, gross=False)
        print(f"L3 {variant} gross ...", flush=True)
        gross = run_variant(inputs, signal, ledger, variant, gross=True)
        net_metrics = core_metrics(net.nav)
        gross_metrics = core_metrics(gross.nav)
        costs = (net.diagnostics["total_commission_twd"]
                 + net.diagnostics["total_transaction_tax_twd"])
        variants[variant] = {
            "gross": {"final_nav": round(float(gross.nav.iloc[-1]), 2),
                      "total_return": round(gross_metrics["total_return"], 4),
                      "cagr": round(gross_metrics["cagr"], 4),
                      "sharpe": round(gross_metrics["sharpe"], 4),
                      "max_drawdown": round(gross_metrics["max_drawdown"], 4)},
            "net": {"final_nav": round(float(net.nav.iloc[-1]), 2),
                    "total_return": round(net_metrics["total_return"], 4),
                    "cagr": round(net_metrics["cagr"], 4),
                    "sharpe": round(net_metrics["sharpe"], 4),
                    "max_drawdown": round(net_metrics["max_drawdown"], 4),
                    "annualised_volatility": round(net_metrics["annualised_volatility"], 4)},
            "cost_drag_cagr_pp": round(
                (gross_metrics["cagr"] - net_metrics["cagr"]) * 100, 3),
            "explicit_costs_twd": round(costs, 2),
            "reconciliation": reconcile(net, capital),
            "distribution": distribution(net),
            "turnover_x_capital_per_year": round(
                net.diagnostics["total_buy_notional_twd"] / capital
                / (net.diagnostics["sessions"] / 252), 3),
        }

    report = {
        "study": "MOM-1 F1 layer decomposition (EXPLORATORY diagnostics)",
        "exploratory": True,
        "note": ("MOM-1 已於 2026-08-14 依 SPEC 9.5 否決。本檔只回答『失敗在哪一層』，"
                 "不新增策略變體、不改參數、不得用於翻案（SPEC 8.3）。"),
        "data_release_id": RELEASE_ID,
        "window": {"start": START, "end": END},
        "cost_model": {"fee_rate_each_side": FEE_RATE, "tax_rate_sell_only": TAX_RATE,
                       "slippage_each_side": 0.003},
        "benchmark_equal_weight_universe": {
            "note": ("SPEC 8.1 基準 2；月頻等權持有當期合格 universe，不計成本。"
                     "用來把動能 edge 與等權/小型股傾斜分開。"),
            "final_nav": round(float(equal_weight.iloc[-1]), 2),
            "total_return": round(float(equal_weight.iloc[-1] / equal_weight.iloc[0] - 1.0), 4),
            "cagr": round(float((equal_weight.iloc[-1] / equal_weight.iloc[0])
                                ** (252 / len(pd.DatetimeIndex(inputs.trading_days)[
                                    (pd.DatetimeIndex(inputs.trading_days) >= pd.Timestamp(START))
                                    & (pd.DatetimeIndex(inputs.trading_days) <= pd.Timestamp(END))])) - 1.0), 4),
        },
        "benchmark_0050": {
            "final_nav": round(float(benchmark.iloc[-1]), 2),
            **{k: round(v, 4) for k, v in core_metrics(benchmark).items()
               if isinstance(v, float)},
        },
        "L1_L2_signal": layers,
        "L3_portfolio": variants,
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                      default=str) + "\n", encoding="utf-8")
    print(f"\nwritten: {args.output}")


if __name__ == "__main__":
    main()
