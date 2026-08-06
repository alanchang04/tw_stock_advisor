"""研究宇宙的市場別限制（EXPERIMENTS.md 2026-08-06「資料覆蓋偏誤」）。

櫃買法人資料 2018-01 才開始，而 development 是 2015~2020，等於同一個研究區間裡
宇宙換過一次。`universe_markets=("TWSE",)` 讓研究維持一致的窄定義。

最重要的是最後一個測試：限制被要求、卻因缺 metadata 而無法套用時必須**大聲失敗**。
今天已經有四個 bug 是「缺資料被預設值無聲吸收」，這個不能再是第五個。
"""
import datetime as dt

import pandas as pd
import pytest

from agent.backtest import _eligible_stock_ids_asof

DAY = dt.date(2024, 6, 3)


def _data(with_stocks: bool = True) -> dict:
    data = {
        "imap": pd.DataFrame([
            {"stock_id": "1111", "industry_code": "SEM"},
            {"stock_id": "3333", "industry_code": "SEM"},
        ]),
        "inds": pd.DataFrame([{"industry_code": "SEM", "name_zh": "半導體"}]),
        "prices": pd.DataFrame([
            {"stock_id": sid, "trade_date": dt.date(2020, 1, 2)}
            for sid in ("1111", "3333")
        ]),
    }
    if with_stocks:
        data["stocks"] = pd.DataFrame([
            {"stock_id": "1111", "market": "TWSE",
             "listing_date": dt.date(2020, 1, 1), "is_active": True},
            {"stock_id": "3333", "market": "TPEX",
             "listing_date": dt.date(2020, 1, 1), "is_active": True},
        ])
    return data


def test_default_keeps_both_venues():
    """正式/實盤預設不限制——實盤資料完整，沒有理由放棄櫃買。"""
    assert _eligible_stock_ids_asof(_data(), DAY) == {"1111", "3333"}


def test_twse_only_drops_tpex_names():
    got = _eligible_stock_ids_asof(_data(), DAY, markets=("TWSE",))
    assert got == {"1111"}


def test_tpex_only_drops_twse_names():
    got = _eligible_stock_ids_asof(_data(), DAY, markets=("TPEX",))
    assert got == {"3333"}


def test_explicit_both_matches_default():
    assert (_eligible_stock_ids_asof(_data(), DAY, markets=("TWSE", "TPEX"))
            == _eligible_stock_ids_asof(_data(), DAY))


def test_restriction_without_market_metadata_raises_instead_of_silently_passing():
    """缺 stocks.parquet 時若靜默忽略限制，報告會宣稱受限而實際未受限。"""
    with pytest.raises(RuntimeError, match="universe_markets"):
        _eligible_stock_ids_asof(_data(with_stocks=False), DAY, markets=("TWSE",))


def test_no_restriction_still_tolerates_missing_metadata():
    """沒有要求限制時，缺 metadata 仍應照舊優雅降級，不可因本次改動而變成錯誤。"""
    assert _eligible_stock_ids_asof(_data(with_stocks=False), DAY) == {"1111", "3333"}
