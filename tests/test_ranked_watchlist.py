"""每日 AI 因子排名清單（2026-07-25）——format 純函式測試。

get_ranked_watchlist() 本身要打 DB（跟 get_candidate_stocks 共用查詢），這裡只測
不需 DB 的格式化函式，確保 Telegram 訊息內容正確、警語不會被拿掉。
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.stock_selector import format_ranked_watchlist_for_telegram


def _df(**over):
    base = dict(stock_id=["2327", "2492"], stock_name=["國巨", "華新科"],
                industry=["電子零組件業", "電子零組件業"], close=[520.0, 180.5],
                score=[9.1, 7.4], invest_streak=[5.0, 0.0], rev_yoy=[31.5, -3.2],
                stack_days=[12.0, 3.0])
    base.update(over)
    return pd.DataFrame(base)


def test_message_lists_all_stocks_with_rank():
    out = format_ranked_watchlist_for_telegram(_df())
    assert "1. 2327 國巨" in out
    assert "2. 2492 華新科" in out
    assert "Top 2" in out


def test_shows_proven_factors_investment_trust_and_revenue():
    """清單必須顯示已驗證的兩個因子（投信連買 + 營收年增），這是它的賣點。"""
    out = format_ranked_watchlist_for_telegram(_df())
    assert "投信連買5日" in out
    assert "營收年增+31.5%" in out


def test_handles_no_streak_and_missing_revenue():
    out = format_ranked_watchlist_for_telegram(
        _df(invest_streak=[0.0, 0.0], rev_yoy=[None, None]))
    assert "投信未連買" in out
    assert "營收無資料" in out


def test_negative_revenue_shown_with_sign():
    out = format_ranked_watchlist_for_telegram(_df())
    assert "營收年增-3.2%" in out


def test_warning_that_it_is_not_a_buy_list_is_present():
    """『這是排名不是買單』的警語不可以消失——那是這頁誠實性的核心。"""
    out = format_ranked_watchlist_for_telegram(_df())
    assert "不是買單" in out
    assert "自己判斷" in out


def test_hard_excluded_count_shown_when_present():
    df = _df()
    df.attrs["hard_excluded"] = [{"stock_id": "3034", "stock_name": "聯詠",
                                  "hard_veto_reason": "乖離月線15%以上；"}]
    out = format_ranked_watchlist_for_telegram(df)
    assert "另有 1 檔" in out


def test_no_hard_excluded_line_when_empty():
    out = format_ranked_watchlist_for_telegram(_df())
    assert "另有" not in out
