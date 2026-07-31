"""Path audit for margin-reversal trades; diagnostics only, never parameter fitting."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from margin_reversal.backtest import run_margin_reversal_backtest
from margin_reversal.data import load_research_frame
from margin_reversal.signals import build_feature_panel, select_signals


def _value(v):
    if pd.isna(v):
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if hasattr(v, "item"):
        return v.item()
    return v


def _record(row):
    return {str(k): _value(v) for k, v in row.items()}


def run(start="2026-04-01", end="2026-07-31", case_stock="2303"):
    frame, market = load_research_frame(start, end)
    panel = build_feature_panel(frame, market)
    selected = select_signals(panel)
    trades = run_margin_reversal_backtest(frame, selected)["trades"]
    paths = []
    horizons = (1, 2, 3, 5, 10, 20)
    for trade in trades.to_dict(orient="records"):
        sid = str(trade["stock_id"])
        prices = frame[frame["stock_id"].astype(str).eq(sid)].sort_values("trade_date")
        after_entry = prices[prices["trade_date"] >= pd.Timestamp(trade["entry_date"])].reset_index(drop=True)
        after_exit = prices[prices["trade_date"] > pd.Timestamp(trade["exit_date"])].reset_index(drop=True)
        entry = float(trade["entry_price"])
        exit_price = float(trade["exit_price"])
        item = _record(trade)
        item["hold_from_entry"] = {
            str(h): (float(after_entry.iloc[h]["close"] / entry - 1)
                     if len(after_entry) > h else None) for h in horizons
        }
        item["rebound_after_exit"] = {
            str(h): (float(after_exit.iloc[h-1]["close"] / exit_price - 1)
                     if len(after_exit) >= h else None) for h in horizons
        }
        first_three = after_entry.head(3)
        dca_cost = float(first_three["open"].mean()) if len(first_three) == 3 else np.nan
        item["three_open_dca_cost"] = _value(dca_cost)
        item["three_open_dca_vs_original"] = (
            float(dca_cost / entry - 1) if not pd.isna(dca_cost) else None
        )
        item["observed_path"] = [
            _record(r) for r in after_entry.head(12)[
                ["trade_date", "open", "high", "low", "close"]
            ].to_dict(orient="records")
        ]
        paths.append(item)

    case = panel[panel["stock_id"].astype(str).eq(str(case_stock))].tail(30)
    case_columns = [
        "stock_id", "trade_date", "close", "margin_balance", "margin_drop_5d",
        "price_drawdown_20d", "revenue_yoy", "inst_buy_streak", "market_stress",
        "margin_wash_event", "days_since_wash", "reversal_confirmed",
        "entry_confirmed", "signal",
    ]
    result = {
        "start": start,
        "end": end,
        "warning": "Exploratory path audit. Future-low entries are intentionally not treated as tradable rules.",
        "trade_paths": paths,
        "case_stock": str(case_stock),
        "case_rows": [_record(r) for r in case[case_columns].to_dict(orient="records")],
        "case_signal_count": int(case["signal"].sum()),
        "case_wash_event_count": int(case["margin_wash_event"].sum()),
    }
    out = Path("reports/margin_reversal_path_audit.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return result


if __name__ == "__main__":
    result = run()
    print(json.dumps({
        "trades": len(result["trade_paths"]),
        "case_stock": result["case_stock"],
        "case_signal_count": result["case_signal_count"],
        "case_wash_event_count": result["case_wash_event_count"],
    }, ensure_ascii=False))
