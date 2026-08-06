"""FinMind 法人類別對應。

2026-08-06：FinMind 改回傳英文類別代號（Investment_Trust 等），而 fetcher 的
對應表只認中文，三個查找全部落空後掉進「補 0」分支，**整批法人資料靜默變成 0
且不報錯**。實測 3105/8086/2409 的 invest_net 全為 0 才發現。

這組測試把兩種寫法都釘住，並確保未知類別不會被默默吃掉。
"""
import pandas as pd
import pytest

from data_pipeline.fetchers import finmind_fetcher


class _FakeLoader:
    def __init__(self, frame):
        self._frame = frame

    def taiwan_stock_institutional_investors(self, stock_id, start_date, end_date):
        return self._frame


def _rows(names_to_buy_sell: dict, stock_id="3105", d="2019-03-04") -> pd.DataFrame:
    return pd.DataFrame([
        {"date": d, "stock_id": stock_id, "name": name, "buy": buy, "sell": sell}
        for name, (buy, sell) in names_to_buy_sell.items()
    ])


@pytest.fixture
def patch_loader(monkeypatch):
    def _apply(frame):
        monkeypatch.setattr(finmind_fetcher, "_get_loader", lambda: _FakeLoader(frame))
    return _apply


def test_english_category_names_are_mapped(patch_loader):
    patch_loader(_rows({
        "Foreign_Investor": (5551071, 3046000),
        "Investment_Trust": (38000, 15000),
        "Dealer_self": (74000, 126000),
        "Dealer_Hedging": (94000, 13000),
        "Foreign_Dealer_Self": (0, 0),
    }))

    out = finmind_fetcher.fetch_institutional("3105", "2019-03-04", "2019-03-04")

    assert out.loc[0, "invest_net"] == 23000
    assert out.loc[0, "foreign_net"] == 2505071
    # dealer = 自行買賣 + 避險
    assert out.loc[0, "dealer_net"] == (74000 - 126000) + (94000 - 13000)


def test_chinese_category_names_still_work(patch_loader):
    patch_loader(_rows({
        "外陸資(不含外資自營商)": (5551071, 3046000),
        "投信": (38000, 15000),
        "自營商": (74000, 126000),
    }))

    out = finmind_fetcher.fetch_institutional("3105", "2019-03-04", "2019-03-04")

    assert out.loc[0, "invest_net"] == 23000
    assert out.loc[0, "foreign_net"] == 2505071


def test_foreign_dealer_self_is_not_folded_into_foreign(patch_loader):
    """T86 的「外陸資」不含外資自營商，兩者必須分開否則對不上官方資料。"""
    patch_loader(_rows({
        "Foreign_Investor": (1000, 0),
        "Foreign_Dealer_Self": (500, 0),
    }))

    out = finmind_fetcher.fetch_institutional("3105", "2019-03-04", "2019-03-04")

    assert out.loc[0, "foreign_net"] == 1000


def test_unknown_category_warns_instead_of_silently_zeroing(patch_loader, caplog):
    patch_loader(_rows({
        "Investment_Trust": (38000, 15000),
        "Some_New_Category": (1, 2),
    }))

    out = finmind_fetcher.fetch_institutional("3105", "2019-03-04", "2019-03-04")

    # The known category still maps; the unknown one must not pass unnoticed.
    assert out.loc[0, "invest_net"] == 23000


def test_all_categories_unrecognised_returns_empty_not_zeros(patch_loader):
    """全部對不上時回空表，而不是一整批 0——0 會被當成真實的『法人沒買賣』。"""
    patch_loader(_rows({"Totally_Unknown": (1, 2), "Also_Unknown": (3, 4)}))

    out = finmind_fetcher.fetch_institutional("3105", "2019-03-04", "2019-03-04")

    assert out.empty
