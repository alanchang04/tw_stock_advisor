"""Normalize TWSE point-in-time issued shares and industry membership."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

import pandas as pd


# Official options exposed by the TWSE MI_QFIIS query page.  Codes 07 and 13
# are parent sectors; their children are preferred when both are present.
INDUSTRY_CATEGORIES = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業",
    "04": "紡織纖維", "05": "電機機械", "06": "電器電纜",
    "07": "化學生技醫療", "21": "化學工業", "22": "生技醫療業",
    "08": "玻璃陶瓷", "09": "造紙工業", "10": "鋼鐵工業",
    "11": "橡膠工業", "12": "汽車工業", "13": "電子工業",
    "24": "半導體業", "25": "電腦及週邊設備業", "26": "光電業",
    "27": "通信網路業", "28": "電子零組件業", "29": "電子通路業",
    "30": "資訊服務業", "31": "其他電子業", "14": "建材營造",
    "15": "航運業", "16": "觀光餐旅", "17": "金融保險",
    "18": "貿易百貨", "23": "油電燃氣業", "19": "綜合", "20": "其他",
}
PARENT_CHILDREN = {
    "07": {"21", "22"},
    "13": {"24", "25", "26", "27", "28", "29", "30", "31"},
}


def parse_mi_qfiis(payload: dict, snapshot_date: date | str) -> pd.DataFrame:
    """Parse an official MI_QFIIS response, preserving shares as integers."""
    if str(payload.get("stat")) != "OK":
        return pd.DataFrame(columns=[
            "snapshot_date", "stock_id", "stock_name", "isin", "issued_shares",
        ])
    # TWSE omits the unit hint when a valid category has zero rows.
    if (payload.get("data") or []) and "股" not in str(payload.get("hints") or ""):
        raise ValueError("MI_QFIIS response does not declare shares as 股")
    fields = [str(value).strip() for value in payload.get("fields") or []]
    required = ["證券代號", "證券名稱", "國際證券編碼", "發行股數"]
    missing = [field for field in required if field not in fields]
    if missing:
        raise ValueError(f"MI_QFIIS missing fields: {missing}")
    positions = {field: fields.index(field) for field in required}
    rows = []
    for raw in payload.get("data") or []:
        shares_text = str(raw[positions["發行股數"]]).replace(",", "").strip()
        if not shares_text.isdigit() or int(shares_text) <= 0:
            raise ValueError(f"invalid issued shares: {shares_text!r}")
        rows.append({
            "snapshot_date": pd.Timestamp(snapshot_date).date().isoformat(),
            "stock_id": str(raw[positions["證券代號"]]).strip(),
            "stock_name": str(raw[positions["證券名稱"]]).strip(),
            "isin": str(raw[positions["國際證券編碼"]]).strip(),
            "issued_shares": int(shares_text),
        })
    frame = pd.DataFrame(rows, columns=[
        "snapshot_date", "stock_id", "stock_name", "isin", "issued_shares",
    ])
    if not frame.empty and frame["stock_id"].duplicated().any():
        raise ValueError("MI_QFIIS contains duplicate stock IDs")
    return frame


def resolve_industry_membership(
    category_stock_ids: dict[str, set[str]],
) -> pd.DataFrame:
    """Resolve official category queries with child-over-parent precedence."""
    memberships: dict[str, set[str]] = defaultdict(set)
    for code, stock_ids in category_stock_ids.items():
        if code not in INDUSTRY_CATEGORIES:
            raise ValueError(f"unknown industry code: {code}")
        for stock_id in stock_ids:
            memberships[str(stock_id)].add(code)

    rows = []
    parent_codes = set(PARENT_CHILDREN)
    for stock_id, codes in memberships.items():
        leaves = codes - parent_codes
        if len(leaves) > 1:
            raise ValueError(
                f"stock {stock_id} belongs to multiple leaf industries: {sorted(leaves)}"
            )
        if leaves:
            selected = next(iter(leaves))
            parents = [
                parent for parent, children in PARENT_CHILDREN.items()
                if selected in children and parent in codes
            ]
            unrelated = codes - {selected, *parents}
            if unrelated:
                raise ValueError(
                    f"stock {stock_id} has unrelated industry memberships: {sorted(codes)}"
                )
        elif len(codes) == 1:
            selected = next(iter(codes))
        else:
            raise ValueError(
                f"stock {stock_id} belongs to multiple parent industries: {sorted(codes)}"
            )
        rows.append({
            "stock_id": stock_id,
            "industry_code_asof": selected,
            "industry_name_asof": INDUSTRY_CATEGORIES[selected],
        })
    return pd.DataFrame(rows).sort_values("stock_id").reset_index(drop=True)


def build_monthly_market_structure(
    issued: pd.DataFrame,
    categories: pd.DataFrame,
    prices: pd.DataFrame,
    security_master: pd.DataFrame,
) -> pd.DataFrame:
    """Join same-day official shares/categories to raw close for common stocks."""
    issued = issued.copy()
    prices = prices.copy()
    issued["stock_id"] = issued["stock_id"].astype(str)
    prices["stock_id"] = prices["stock_id"].astype(str)
    master = security_master[["stock_id", "asset_type"]].copy()
    master["stock_id"] = master["stock_id"].astype(str)
    result = issued.merge(categories, on="stock_id", how="left", validate="one_to_one")
    result = result.merge(
        prices[["stock_id", "trade_date", "close"]],
        left_on=["stock_id", "snapshot_date"],
        right_on=["stock_id", "trade_date"],
        how="left", validate="one_to_one",
    ).merge(master, on="stock_id", how="left", validate="many_to_one")
    result = result[result["asset_type"].eq("common_stock")].copy()
    result["issued_shares"] = pd.to_numeric(
        result["issued_shares"], errors="raise", downcast=None
    ).astype("int64")
    if result["issued_shares"].le(0).any():
        raise ValueError("issued shares must be positive")
    result["close"] = pd.to_numeric(result["close"], errors="coerce")
    result["market_cap_twd"] = result["issued_shares"] * result["close"]
    result["industry_is_point_in_time"] = result["industry_code_asof"].notna()
    result["issued_shares_unit"] = "shares"
    result["market_cap_unit"] = "TWD"
    result["issued_shares_source"] = "TWSE_MI_QFIIS"
    result["industry_source"] = "TWSE_MI_QFIIS_category_query"
    result["market_cap_method"] = "issued_shares_x_same_day_raw_close"
    return result[[
        "snapshot_date", "stock_id", "stock_name", "isin", "asset_type",
        "issued_shares", "issued_shares_unit", "close", "market_cap_twd",
        "market_cap_unit", "industry_code_asof", "industry_name_asof",
        "industry_is_point_in_time", "issued_shares_source", "industry_source",
        "market_cap_method",
    ]].sort_values(["snapshot_date", "stock_id"]).reset_index(drop=True)
