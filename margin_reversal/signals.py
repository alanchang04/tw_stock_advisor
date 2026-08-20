"""Point-in-time signal construction for margin-wash reversals."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import MARGIN_REVERSAL_CONFIG

REQUIRED_COLUMNS = {
    "stock_id", "trade_date", "close", "margin_balance", "inst_net", "revenue_yoy"
}


def build_feature_panel(frame: pd.DataFrame, market: pd.Series,
                        cfg: dict | None = None) -> pd.DataFrame:
    """Build only backward-looking features; forward returns are intentionally absent."""
    cfg = {**MARGIN_REVERSAL_CONFIG, **(cfg or {})}
    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"missing columns: {sorted(missing)}")
    df = frame.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["stock_id", "trade_date"])
    g = df.groupby("stock_id", group_keys=False)
    df["price_drawdown_20d"] = df["close"] / g["close"].transform(
        lambda s: s.rolling(20, min_periods=10).max()) - 1
    df["margin_drop_5d"] = g["margin_balance"].pct_change(5)
    df["inst_buy_streak"] = g["inst_net"].transform(
        lambda s: s.gt(0).astype(int).groupby(s.le(0).cumsum()).cumsum())
    if "turnover" in df:
        df["avg_turnover_5d"] = g["turnover"].transform(
            lambda s: s.rolling(5, min_periods=3).mean())
    else:
        df["avg_turnover_5d"] = np.inf

    m = pd.to_numeric(market, errors="coerce").sort_index()
    m.index = pd.to_datetime(m.index)
    market_features = pd.DataFrame({
        "market_daily_return": m.pct_change(),
        "market_drawdown_20d": m / m.rolling(20, min_periods=10).max() - 1,
    })
    df = df.merge(market_features, left_on="trade_date", right_index=True, how="left")
    df["market_stress"] = ((df["market_daily_return"] <= cfg["market_daily_drop"]) |
                           (df["market_drawdown_20d"] <= cfg["market_drawdown_20d"]))
    df["margin_wash"] = ((df["margin_drop_5d"] <= cfg["margin_drop_5d"]) &
                         (df["margin_balance"] >= cfg["min_margin_balance"]))
    rsi_ok = df["rsi14"].le(cfg["max_rsi"]) if "rsi14" in df else True
    df["price_oversold"] = ((df["price_drawdown_20d"] <= cfg["price_drawdown_20d"]) & rsi_ok)
    df["fundamental_ok"] = df["revenue_yoy"].ge(cfg["min_revenue_yoy"])
    df["institutional_ok"] = df["inst_buy_streak"].ge(cfg["institutional_buy_days"])
    df["institutional_recent"] = df["institutional_ok"].groupby(
        df["stock_id"]
    ).transform(lambda s: s.rolling(
        cfg["confirmation_max_days"], min_periods=1).max().astype(bool))
    df["liquid"] = df["avg_turnover_5d"].ge(cfg["min_avg_turnover"])
    g2 = df.groupby("stock_id", group_keys=False)
    prev_wash = g2["margin_wash"].shift(1).fillna(False).astype(bool)
    df["margin_wash_event"] = df["margin_wash"] & ~prev_wash
    event_base = (df["margin_wash_event"] & df["market_stress"] &
                  df["price_oversold"] & df["fundamental_ok"] & df["liquid"])

    def _days_since_event(s: pd.Series) -> pd.Series:
        pos = pd.Series(np.arange(len(s), dtype=float), index=s.index)
        return pos - pos.where(s.astype(bool)).ffill()

    df["days_since_wash"] = event_base.groupby(
        df["stock_id"], group_keys=False
    ).transform(_days_since_event)
    df["margin_wash_recent"] = df["days_since_wash"].between(
        cfg["confirmation_min_days"], cfg["confirmation_max_days"])
    low = df["low"] if "low" in df else df["close"]
    high = df["high"] if "high" in df else df["close"]
    prior_low = low.groupby(df["stock_id"]).transform(
        lambda s: s.shift(1).rolling(
            cfg["higher_low_lookback"], min_periods=2).min())
    prior_high = high.groupby(df["stock_id"]).shift(1)
    ma5 = g2["close"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    df["higher_low"] = low > prior_low
    df["bullish_break"] = ((df["close"] > prior_high) &
                           (df["close"] > g2["close"].shift(1)))
    df["above_ma5"] = df["close"] > ma5
    price_confirmation = (
        df["bullish_break"] & df["above_ma5"]
        if cfg["require_close_above_ma5"]
        else (df["bullish_break"] | df["above_ma5"])
    )
    df["reversal_confirmed"] = df["higher_low"] & price_confirmation
    df["entry_confirmed"] = (
        df["reversal_confirmed"] & df["price_oversold"] &
        df["fundamental_ok"] & df["institutional_recent"] & df["liquid"]
    )
    df["score"] = (
        (-df["margin_drop_5d"].clip(-1, 0) * 35).fillna(0)
        + (-df["price_drawdown_20d"].clip(-1, 0) * 25).fillna(0)
        + df["revenue_yoy"].clip(0, 100).fillna(0) * 0.15
        + df["inst_buy_streak"].clip(0, 5).fillna(0) * 3
    )
    df["signal"] = df["margin_wash_recent"] & df["entry_confirmed"]
    return df


def select_signals(panel: pd.DataFrame, top_n: int = 5) -> pd.DataFrame:
    """Rank independent same-day candidates; ETF filtering is structural."""
    df = panel[panel["signal"]].copy()
    df = df[df["stock_id"].astype(str).str.fullmatch(r"\d{4}")]
    if "asset_type" in df:
        df = df[df["asset_type"].fillna("common_stock").eq("common_stock")]
    return (df.sort_values(["trade_date", "score"], ascending=[True, False])
              .groupby("trade_date", group_keys=False).head(top_n))
