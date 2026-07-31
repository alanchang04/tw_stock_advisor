"""Point-in-time research dataset loader."""
from __future__ import annotations

import pandas as pd
from sqlalchemy import text

from database.connection import get_session


def _revenue_available_date(year_month: str) -> pd.Timestamp:
    period = pd.Period(str(year_month), freq="M")
    return (period + 1).start_time + pd.Timedelta(days=9)


def attach_point_in_time_revenue(frame: pd.DataFrame, revenue: pd.DataFrame) -> pd.DataFrame:
    """Attach latest revenue only after its statutory next-month publication date."""
    if revenue.empty:
        out = frame.copy(); out["revenue_yoy"] = pd.NA; return out
    rev = revenue.copy()
    rev["available_date"] = pd.to_datetime(
        rev["year_month"].map(_revenue_available_date)
    ).astype("datetime64[ns]")
    frame = frame.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).astype("datetime64[ns]")
    pieces = []
    for sid, group in frame.groupby("stock_id", sort=False):
        rr = rev[rev["stock_id"].astype(str).eq(str(sid))].sort_values("available_date")
        gg = group.sort_values("trade_date")
        if rr.empty:
            gg = gg.copy(); gg["revenue_yoy"] = pd.NA
        else:
            gg = pd.merge_asof(gg, rr[["available_date", "yoy_pct"]],
                               left_on="trade_date", right_on="available_date",
                               direction="backward").rename(columns={"yoy_pct": "revenue_yoy"})
        pieces.append(gg)
    return pd.concat(pieces, ignore_index=True) if pieces else frame.assign(revenue_yoy=pd.NA)


def load_research_frame(start_date, end_date) -> tuple[pd.DataFrame, pd.Series]:
    params = {"start": start_date, "end": end_date}
    with get_session() as s:
        frame = pd.read_sql(text("""
            SELECT p.stock_id, p.trade_date, p.open, p.high, p.low, p.close,
                   p.turnover, t.rsi14, m.margin_balance,
                   COALESCE(i.total_net, 0) AS inst_net,
                   CASE WHEN st.stock_id ~ '^[0-9]{4}$'
                             AND COALESCE(ind.name_zh, '') NOT ILIKE '%ETF%'
                        THEN 'common_stock' ELSE 'other' END AS asset_type
            FROM daily_prices p
            JOIN stocks st ON st.stock_id=p.stock_id
            JOIN margin_trading m ON m.stock_id=p.stock_id AND m.trade_date=p.trade_date
            LEFT JOIN technical_indicators t ON t.stock_id=p.stock_id AND t.trade_date=p.trade_date
            LEFT JOIN institutional_trading i ON i.stock_id=p.stock_id AND i.trade_date=p.trade_date
            LEFT JOIN LATERAL (
                SELECT inds.name_zh FROM stock_industry_map sm
                JOIN industries inds ON inds.code=sm.industry_code
                WHERE sm.stock_id=p.stock_id ORDER BY inds.code LIMIT 1
            ) ind ON TRUE
            WHERE p.trade_date BETWEEN :start AND :end
        """), s.bind, params=params)
        revenue = pd.read_sql(text("""
            SELECT stock_id, year_month, yoy_pct FROM monthly_revenue
        """), s.bind)
        market = pd.read_sql(text("""
            SELECT trade_date, close FROM daily_prices
            WHERE stock_id='0050' AND trade_date BETWEEN :start AND :end
            ORDER BY trade_date
        """), s.bind, params=params)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = attach_point_in_time_revenue(frame, revenue)
    market["trade_date"] = pd.to_datetime(market["trade_date"])
    return frame, market.set_index("trade_date")["close"]
