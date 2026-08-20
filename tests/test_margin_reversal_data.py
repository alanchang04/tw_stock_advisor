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


def test_the_deadline_day_itself_is_still_not_visible():
    """M23：期限日**當天**也看不到。

    上面那個測試用 2/9 與 2/12，**刻意跳過了 2/10 本身**，因此舊實作
    （把可得時點設在 2/10 00:00，as-of 回溯就在當天配上）一樣會通過。
    法規只說「10 日以前申報」不規範時刻——公司可能 10 日盤後才申報，
    所以用 2/10 的價格交易是偷看一天。邊界要自己測。
    """
    frame = pd.DataFrame({"stock_id": ["1111", "1111"],
                          "trade_date": pd.to_datetime(["2024-02-10", "2024-02-11"])})
    revenue = pd.DataFrame({"stock_id": ["1111"], "year_month": ["2024-01"],
                            "yoy_pct": [12.0]})
    out = attach_point_in_time_revenue(frame, revenue)
    assert pd.isna(out.loc[0, "revenue_yoy"]), "2/10 當天不得看到 1 月營收"
    assert out.loc[1, "revenue_yoy"] == 12.0
