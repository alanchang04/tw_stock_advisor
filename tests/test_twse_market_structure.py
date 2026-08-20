import pandas as pd
import pytest

from research.twse_market_structure import (
    build_monthly_market_structure,
    parse_mi_qfiis,
    resolve_industry_membership,
)


def test_mi_qfiis_issued_shares_are_not_divided_into_lots():
    payload = {
        "stat": "OK", "hints": "單位:股",
        "fields": ["證券代號", "證券名稱", "國際證券編碼", "發行股數"],
        "data": [["1101", "台泥", "TW0001101004", "2,645,764,407"]],
    }

    frame = parse_mi_qfiis(payload, "2005-01-03")

    assert frame.iloc[0]["issued_shares"] == 2_645_764_407


def test_mi_qfiis_requires_official_share_unit():
    payload = {
        "stat": "OK", "hints": "單位:張",
        "fields": ["證券代號", "證券名稱", "國際證券編碼", "發行股數"],
        "data": [["1101", "台泥", "TW0001101004", "2,645,764"]],
    }

    with pytest.raises(ValueError, match="does not declare shares as 股"):
        parse_mi_qfiis(payload, "2005-01-03")


def test_industry_child_category_overrides_parent_without_guessing():
    result = resolve_industry_membership({
        "13": {"2301", "2330"},
        "24": {"2330"},
        "25": {"2301"},
    }).set_index("stock_id")

    assert result.loc["2330", "industry_code_asof"] == "24"
    assert result.loc["2301", "industry_code_asof"] == "25"


def test_market_cap_uses_same_day_raw_close_and_integer_shares():
    issued = pd.DataFrame([{
        "snapshot_date": "2005-01-03", "stock_id": "1101", "stock_name": "台泥",
        "isin": "TW0001101004", "issued_shares": 2_645_764_407,
    }])
    categories = pd.DataFrame([{
        "stock_id": "1101", "industry_code_asof": "01",
        "industry_name_asof": "水泥工業",
    }])
    prices = pd.DataFrame([{
        "stock_id": "1101", "trade_date": "2005-01-03", "close": 20.5,
    }])
    master = pd.DataFrame([{"stock_id": "1101", "asset_type": "common_stock"}])

    result = build_monthly_market_structure(issued, categories, prices, master).iloc[0]

    assert result["issued_shares"] == 2_645_764_407
    assert result["market_cap_twd"] == pytest.approx(2_645_764_407 * 20.5)
    assert result["issued_shares_unit"] == "shares"
