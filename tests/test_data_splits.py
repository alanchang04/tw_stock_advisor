"""SPEC_QUANT_UPGRADE §4.1 資料三分的守門測試。

重點不是「函式會不會動」，而是「切分邊界有沒有被偷改」——規格書規定切了不可再動，
所以邊界日期本身就該有測試釘住，改動時會亮紅燈逼人回來看規格書。
"""
from datetime import date

import pytest

from research import data_splits as ds


# ── 邊界釘死（§4.1 使用者決策 2026-07-24，不可再動）──────────────
def test_split_boundaries_are_frozen():
    assert ds.DEVELOPMENT == (date(2015, 1, 1), date(2020, 12, 31))
    assert ds.VALIDATION == (date(2021, 1, 1), date(2022, 12, 31))
    assert ds.HOLDOUT == (date(2023, 1, 1), date(2025, 5, 31))
    assert ds.CONTAMINATED[0] == date(2025, 6, 1)


def test_splits_do_not_overlap_and_leave_no_gap():
    order = [ds.DEVELOPMENT, ds.VALIDATION, ds.HOLDOUT, ds.CONTAMINATED]
    for (_, hi), (lo, _) in zip(order, order[1:]):
        assert (lo - hi).days == 1, f"{hi} 與 {lo} 之間有斷層或重疊"


@pytest.mark.parametrize("d,expected", [
    (date(2015, 1, 1), "development"),
    (date(2020, 12, 31), "development"),
    (date(2021, 1, 1), "validation"),
    (date(2022, 12, 31), "validation"),
    (date(2023, 1, 1), "holdout"),
    (date(2025, 5, 31), "holdout"),
    # 現行策略的調參目標窗＝硬污染，絕不可被判成 holdout
    (date(2025, 6, 1), "contaminated"),
    (date(2026, 7, 24), "contaminated"),
])
def test_split_of(d, expected):
    assert ds.split_of(d) == expected


def test_dates_before_data_start_are_unknown():
    assert ds.split_of(date(2014, 12, 31)) == "unknown"


# ── 記帳機制（§4「制度、不靠自律」的實作）─────────────────────────
def test_slice_dates_filters_and_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "_LOG", tmp_path / "log.md")
    dates = [date(2020, 6, 1), date(2021, 6, 1), date(2024, 6, 1), date(2026, 6, 1)]

    assert ds.slice_dates(dates, "validation", "單元測試") == [date(2021, 6, 1)]
    assert ds.access_count("validation") == 1
    assert ds.access_count("holdout") == 0

    ds.slice_dates(dates, "holdout", "單元測試")
    assert ds.access_count("holdout") == 1


def test_development_access_is_not_tracked(tmp_path, monkeypatch):
    """dev 隨便玩，不該被記帳污染稽核紀錄。"""
    monkeypatch.setattr(ds, "_LOG", tmp_path / "log.md")
    ds.slice_dates([date(2018, 1, 2)], "development", "單元測試")
    assert ds.access_count("development") == 0
    assert not (tmp_path / "log.md").exists()


def test_unknown_split_raises():
    with pytest.raises(ValueError):
        ds.slice_dates([], "test_set")


def test_holdout_caveat_mentions_contamination():
    """holdout 已被五輪 A/B 間接污染，這個警語不可以被悄悄拿掉。"""
    assert "污染" in ds.HOLDOUT_CAVEAT
    assert "前向" in ds.HOLDOUT_CAVEAT
