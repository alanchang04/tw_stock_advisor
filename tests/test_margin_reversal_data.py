import pandas as pd

from margin_reversal.data import attach_point_in_time_revenue


def test_revenue_is_not_visible_before_next_month_tenth():
    frame = pd.DataFrame({"stock_id": ["1111", "1111"],
                          "trade_date": pd.to_datetime(["2024-02-09", "2024-02-12"])})
    revenue = pd.DataFrame({"stock_id": ["1111"], "year_month": ["2024-01"],
                            "yoy_pct": [12.0]})
    out = attach_point_in_time_revenue(frame, revenue)
    assert pd.isna(out.loc[0, "revenue_yoy"])
    assert out.loc[1, "revenue_yoy"] == 12.0
