"""
daily_digest.digest_age_days() 測試。

2026-07-21新增：get_latest_digest() 有5天內回退機制，當天彙整生成失敗時會
悄悄顯示最近一次成功的舊彙整，之前使用者誤以為是「系統認錯日期」的bug回報過
（其實tw_today()完全正確，只是顯示端沒標示資料是幾天前的）。這裡只測純函式
天數計算本身，不需要DB。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_pipeline.analysis.daily_digest import digest_age_days


def test_same_day_is_zero_age():
    d = date(2026, 7, 20)
    assert digest_age_days(d, as_of=d) == 0


def test_stale_digest_reports_positive_age():
    digest_date = date(2026, 7, 17)
    as_of = date(2026, 7, 20)
    assert digest_age_days(digest_date, as_of=as_of) == 3


def test_defaults_as_of_to_tw_today(monkeypatch):
    import data_pipeline.analysis.daily_digest as dd
    monkeypatch.setattr(dd, "tw_today", lambda: date(2026, 7, 21))
    assert digest_age_days(date(2026, 7, 19)) == 2
