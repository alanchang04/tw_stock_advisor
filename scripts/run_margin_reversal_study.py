"""Run independent margin-reversal event study and backtest."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.performance import buy_and_hold_nav
from agent.backtest import perf_metrics
from margin_reversal.data import load_research_frame
from margin_reversal.signals import build_feature_panel, select_signals
from margin_reversal.event_study import add_forward_returns, matched_margin_wash_study
from margin_reversal.backtest import run_margin_reversal_backtest
from margin_reversal.report import format_event_study


def _safe(v):
    if hasattr(v, "item"): return v.item()
    if hasattr(v, "isoformat"): return v.isoformat()
    return v


def run(start, end, out_dir="reports") -> dict:
    frame, market = load_research_frame(start, end)
    panel = build_feature_panel(frame, market)
    selected = select_signals(panel)
    gate_counts = {
        "rows": len(panel),
        "market_stress": int(panel["market_stress"].sum()),
        "price_oversold": int(panel["price_oversold"].sum()),
        "margin_wash": int(panel["margin_wash"].sum()),
        "fundamental_ok": int(panel["fundamental_ok"].sum()),
        "institutional_ok": int(panel["institutional_ok"].sum()),
        "wash_events": int(panel["margin_wash_event"].sum()),
        "reversal_confirmed": int(panel["reversal_confirmed"].sum()),
        "entry_confirmed": int(panel["entry_confirmed"].sum()),
        "confirmed_after_wash": int(panel["signal"].sum()),
        "stress_oversold": int((panel["market_stress"] & panel["price_oversold"]).sum()),
        "base_quality": int((panel["market_stress"] & panel["price_oversold"] &
                             panel["fundamental_ok"] & panel["institutional_ok"]).sum()),
        "all_signal_gates": int(panel["signal"].sum()),
    }
    study = {h: matched_margin_wash_study(add_forward_returns(panel, (h,)), h)
             for h in (1, 3, 5, 10)}
    bt = run_margin_reversal_backtest(frame, selected)
    trades = bt["trades"]
    trade_summary = {
        "wins": int((trades["net_return"] > 0).sum()) if not trades.empty else 0,
        "losses": int((trades["net_return"] <= 0).sum()) if not trades.empty else 0,
        "win_rate": float((trades["net_return"] > 0).mean()) if not trades.empty else 0.0,
        "average_return": float(trades["net_return"].mean()) if not trades.empty else 0.0,
        "median_return": float(trades["net_return"].median()) if not trades.empty else 0.0,
        "best_return": float(trades["net_return"].max()) if not trades.empty else 0.0,
        "worst_return": float(trades["net_return"].min()) if not trades.empty else 0.0,
        "reasons": (trades.groupby("reason")["net_return"].agg(["count", "mean"])
                    .to_dict(orient="index") if not trades.empty else {}),
    }
    bench_nav = buy_and_hold_nav(market.reindex(bt["nav"].index).ffill(),
                                 bt["nav"].iloc[0] if len(bt["nav"]) else 0)
    result = {"start": start, "end": end, "data_rows": len(frame),
              "signal_count": len(selected), "trade_count": len(bt["trades"]),
              "gate_counts": gate_counts,
              "trade_summary": trade_summary,
              "trade_details": trades.to_dict(orient="records"),
              "selected_signals": selected[[
                  c for c in ("stock_id", "trade_date", "score", "days_since_wash",
                              "margin_change_5d", "drawdown_20d", "revenue_yoy",
                              "inst_buy_2d") if c in selected.columns
              ]].to_dict(orient="records"),
              "strategy_metrics": bt["metrics"],
              "benchmark_metrics": perf_metrics(bench_nav), "event_study": study}
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    (out / "margin_reversal_study.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=_safe), encoding="utf-8")
    lines = ["# Margin Reversal 研究報告", "", f"期間：{start}～{end}",
             f"資料列：{len(frame):,}；訊號：{len(selected):,}；交易：{len(bt['trades']):,}", ""]
    lines += [format_event_study(study[h]) for h in (1, 3, 5, 10)]
    (out / "margin_reversal_study.md").write_text("\n\n".join(lines), encoding="utf-8")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", required=True)
    ap.add_argument("--out-dir", default="reports")
    a = ap.parse_args(); print(json.dumps(run(a.start, a.end, a.out_dir), default=_safe))
