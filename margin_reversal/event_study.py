"""Matched event study: margin wash versus similar oversold non-wash stocks."""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats


def add_forward_returns(panel: pd.DataFrame, horizons=(1, 3, 5, 10)) -> pd.DataFrame:
    out = panel.sort_values(["stock_id", "trade_date"]).copy()
    g = out.groupby("stock_id")["close"]
    for h in horizons:
        out[f"fwd_{h}d"] = g.shift(-h) / out["close"] - 1
    return out


def matched_margin_wash_study(panel: pd.DataFrame, horizon: int = 5,
                              caliper: float = 0.05, seed: int = 42) -> dict:
    """Match on date, price drawdown and fundamentals; treatment is margin wash."""
    ret_col = f"fwd_{horizon}d"
    if ret_col not in panel:
        panel = add_forward_returns(panel, (horizon,))
    if "entry_confirmed" in panel:
        eligible = panel[panel["entry_confirmed"] & panel[ret_col].notna()].copy()
        treatment = "margin_wash_recent"
    else:
        eligible = panel[(panel["market_stress"]) & (panel["price_oversold"]) &
                         (panel["fundamental_ok"]) & (panel["institutional_ok"]) &
                         panel[ret_col].notna()].copy()
        treatment = "margin_wash"
    treated = eligible[eligible[treatment]]
    controls = eligible[~eligible[treatment]]
    pairs = []
    for tr in treated.itertuples():
        delta_days = (pd.to_datetime(controls["trade_date"]) -
                      pd.Timestamp(tr.trade_date)).dt.days.abs()
        same = controls[delta_days.le(3)].copy()
        if same.empty:
            continue
        same_delta = (pd.to_datetime(same["trade_date"]) -
                      pd.Timestamp(tr.trade_date)).dt.days.abs()
        same["distance"] = ((same["price_drawdown_20d"] - tr.price_drawdown_20d).abs() +
                            (same["revenue_yoy"] - tr.revenue_yoy).abs()/100 +
                            same_delta * .005)
        co = same.sort_values("distance").iloc[0]
        if co["distance"] <= caliper:
            pairs.append((getattr(tr, ret_col), co[ret_col]))
    if not pairs:
        return {"horizon": horizon, "treated_n": 0, "control_n": 0,
                "treated_mean": np.nan, "control_mean": np.nan,
                "lift": np.nan, "p_value": np.nan, "significant": False}
    arr = np.asarray(pairs, dtype=float); diff = arr[:, 0] - arr[:, 1]
    _, p = stats.ttest_1samp(diff, 0.0) if len(diff) >= 2 else (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    boot = rng.choice(diff, size=(5000, len(diff)), replace=True).mean(axis=1)
    lo, hi = np.percentile(boot, [2.5, 97.5])
    significant_difference = bool((lo > 0 or hi < 0) and p < .05)
    return {"horizon": horizon, "treated_n": len(diff), "control_n": len(diff),
            "treated_mean": float(arr[:, 0].mean()), "control_mean": float(arr[:, 1].mean()),
            "lift": float(diff.mean()), "ci_low": float(lo), "ci_high": float(hi),
            "p_value": float(p), "significant": bool(lo > 0 and p < .05),
            "significant_difference": significant_difference,
            "direction": "better" if lo > 0 else "worse" if hi < 0 else "inconclusive"}
