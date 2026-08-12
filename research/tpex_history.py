"""Normalize official TPEx history without inventing values for missing fields."""
from __future__ import annotations

from datetime import date
import re

import pandas as pd


PRICE_COLUMNS = [
    "stock_id", "stock_name", "trade_date", "open", "high", "low", "close",
    "volume", "turnover", "issued_shares", "market", "quote_table",
    "price_missing", "volume_unit", "turnover_unit", "issued_shares_unit",
]


def roc_date(value: object) -> date | None:
    """Parse ROC dates emitted by TPEx (YYY/MM/DD or YYY-MM-DD)."""
    text = "" if value is None else str(value).strip()
    match = re.fullmatch(r"(\d{2,3})[/-](\d{1,2})[/-](\d{1,2})", text)
    if not match:
        return None
    year, month, day = map(int, match.groups())
    try:
        return date(year + 1911, month, day)
    except ValueError:
        return None


def number(value: object) -> float | None:
    text = "" if value is None else str(value).strip().replace(",", "")
    if text in {"", "-", "--", "---", "----", "N/A", "nan"}:
        return None
    text = text.replace("+", "")
    try:
        return float(text)
    except ValueError:
        return None


def integer(value: object) -> int | None:
    parsed = number(value)
    if parsed is None:
        return None
    if not float(parsed).is_integer():
        raise ValueError(f"expected integer, got {value!r}")
    return int(parsed)


def positive_price(value: object) -> float | None:
    parsed = number(value)
    return parsed if parsed is not None and parsed > 0 else None


def _table(payload: dict, required_field: str) -> dict | None:
    for table in payload.get("tables") or []:
        if required_field in [str(field).strip() for field in table.get("fields") or []]:
            return table
    return None


def parse_daily_quotes(payload: dict, trade_date: date) -> pd.DataFrame:
    """Parse standard listed-board quotes; retain suspended rows with null prices.

    TPEx returns ETFs, warrants and other instruments in the same table.  The
    exchange's four-digit numeric equity-code convention isolates listed shares;
    the official historical listed/delisted universe is cross-checked downstream.
    """
    table = _table(payload, "成交股數")
    if table is None:
        return pd.DataFrame(columns=PRICE_COLUMNS)
    fields = [str(field).strip() for field in table.get("fields") or []]
    required = [
        "代號", "名稱", "收盤", "開盤", "最高", "最低", "成交股數",
        "成交金額(元)", "發行股數",
    ]
    missing = [field for field in required if field not in fields]
    if missing:
        raise ValueError(f"TPEx dailyQuotes missing fields: {missing}")
    at = {field: fields.index(field) for field in required}
    rows = []
    for raw in table.get("data") or []:
        stock_id = str(raw[at["代號"]]).strip()
        if not re.fullmatch(r"\d{4}", stock_id):
            continue
        close = positive_price(raw[at["收盤"]])
        volume = integer(raw[at["成交股數"]])
        turnover = integer(raw[at["成交金額(元)"]])
        shares = integer(raw[at["發行股數"]])
        rows.append({
            "stock_id": stock_id,
            "stock_name": str(raw[at["名稱"]]).strip(),
            "trade_date": trade_date.isoformat(),
            "open": positive_price(raw[at["開盤"]]),
            "high": positive_price(raw[at["最高"]]),
            "low": positive_price(raw[at["最低"]]),
            "close": close,
            "volume": volume,
            "turnover": turnover,
            "issued_shares": shares,
            "market": "TPEX",
            "quote_table": str(table.get("title") or "上櫃股票行情"),
            "price_missing": close is None,
            "volume_unit": "shares",
            "turnover_unit": "TWD",
            "issued_shares_unit": "shares",
        })
    result = pd.DataFrame(rows, columns=PRICE_COLUMNS)
    if not result.empty and result.duplicated(["stock_id", "trade_date"]).any():
        raise ValueError("TPEx dailyQuotes contains duplicate common-stock rows")
    return result


def parse_ex_daily(payload: dict) -> pd.DataFrame:
    table = _table(payload, "除權息日期")
    columns = [
        "stock_id", "ex_date", "stock_name", "pre_close", "ref_price",
        "stock_dividend_value", "cash_dividend", "event_type", "market",
        "price_unit", "stock_dividend_value_unit",
    ]
    if table is None:
        return pd.DataFrame(columns=columns)
    fields = [str(field).strip() for field in table.get("fields") or []]
    required = [
        "除權息日期", "代號", "名稱", "除權息前收盤價", "除權息參考價",
        "權值", "息值", "權/息",
    ]
    missing = [field for field in required if field not in fields]
    if missing:
        raise ValueError(f"TPEx exDailyQ missing fields: {missing}")
    at = {field: fields.index(field) for field in required}
    rows = []
    for raw in table.get("data") or []:
        stock_id = str(raw[at["代號"]]).strip()
        event_date = roc_date(raw[at["除權息日期"]])
        if event_date is None or not re.fullmatch(r"\d{4}", stock_id):
            continue
        rows.append({
            "stock_id": stock_id,
            "ex_date": event_date.isoformat(),
            "stock_name": str(raw[at["名稱"]]).strip(),
            "pre_close": number(raw[at["除權息前收盤價"]]),
            "ref_price": number(raw[at["除權息參考價"]]),
            # TPEx calls this 權值: it is a TWD/share reference-price deduction,
            # not a share ratio.  Naming it *_ratio caused an old unit ambiguity.
            "stock_dividend_value": number(raw[at["權值"]]),
            "cash_dividend": number(raw[at["息值"]]),
            "event_type": str(raw[at["權/息"]]).strip(),
            "market": "TPEX",
            "price_unit": "TWD_per_share",
            "stock_dividend_value_unit": "TWD_per_share_reference_deduction",
        })
    return pd.DataFrame(rows, columns=columns)


def parse_delisted(payload: dict) -> pd.DataFrame:
    table = _table(payload, "終止上櫃日期")
    columns = ["stock_id", "stock_name", "delisting_date", "reason", "market"]
    if table is None:
        return pd.DataFrame(columns=columns)
    fields = [str(field).strip() for field in table.get("fields") or []]
    required = ["股票代號", "公司名稱", "終止上櫃日期", "終止上櫃原因"]
    at = {field: fields.index(field) for field in required}
    rows = []
    for raw in table.get("data") or []:
        stock_id = str(raw[at["股票代號"]]).strip()
        event_date = roc_date(raw[at["終止上櫃日期"]])
        if event_date and re.fullmatch(r"\d{4}", stock_id):
            rows.append({
                "stock_id": stock_id,
                "stock_name": str(raw[at["公司名稱"]]).strip(),
                "delisting_date": event_date.isoformat(),
                "reason": str(raw[at["終止上櫃原因"]]).strip(),
                "market": "TPEX",
            })
    return pd.DataFrame(rows, columns=columns)


def parse_attention(payload: dict) -> pd.DataFrame:
    table = _table(payload, "注意交易資訊")
    columns = [
        "stock_id", "notice_date", "stock_name", "reason", "notice_count",
        "close", "pe_ratio", "market",
    ]
    if table is None:
        return pd.DataFrame(columns=columns)
    fields = [str(field).strip() for field in table.get("fields") or []]
    required = ["證券代號", "證券名稱", "累計", "注意交易資訊", "公告日期", "收盤價", "本益比"]
    at = {field: fields.index(field) for field in required}
    rows = []
    for raw in table.get("data") or []:
        stock_id = str(raw[at["證券代號"]]).strip()
        event_date = roc_date(raw[at["公告日期"]])
        if event_date and re.fullmatch(r"\d{4}", stock_id):
            rows.append({
                "stock_id": stock_id,
                "notice_date": event_date.isoformat(),
                "stock_name": str(raw[at["證券名稱"]]).strip(),
                "reason": str(raw[at["注意交易資訊"]]).strip(),
                "notice_count": integer(raw[at["累計"]]),
                "close": number(raw[at["收盤價"]]),
                "pe_ratio": number(raw[at["本益比"]]),
                "market": "TPEX",
            })
    return pd.DataFrame(rows, columns=columns)


def parse_disposal(payload: dict) -> pd.DataFrame:
    table = _table(payload, "處置起訖時間")
    columns = [
        "event_no", "stock_id", "stock_name", "announcement_date", "start_date",
        "end_date", "occurrence_count", "reason", "measure", "close", "pe_ratio",
        "market",
    ]
    if table is None:
        return pd.DataFrame(columns=columns)
    fields = [str(field).strip() for field in table.get("fields") or []]
    required = [
        "編號", "公布日期", "證券代號", "證券名稱", "累計", "處置起訖時間",
        "處置原因", "處置內容", "收盤價", "本益比",
    ]
    at = {field: fields.index(field) for field in required}
    rows = []
    for raw in table.get("data") or []:
        stock_id = str(raw[at["證券代號"]]).strip()
        announced = roc_date(raw[at["公布日期"]])
        if announced is None or not re.fullmatch(r"\d{4}", stock_id):
            continue
        period = str(raw[at["處置起訖時間"]]).strip()
        dates = re.findall(r"\d{2,3}[/-]\d{1,2}[/-]\d{1,2}", period)
        start = roc_date(dates[0]) if dates else None
        end = roc_date(dates[1]) if len(dates) > 1 else None
        if start is None or end is None:
            raise ValueError(f"unparseable TPEx disposal period: {period!r}")
        name = re.sub(r"\([^)]*company-detail[^)]*\)$", "", str(raw[at["證券名稱"]])).strip()
        rows.append({
            "event_no": integer(raw[at["編號"]]),
            "stock_id": stock_id,
            "stock_name": name,
            "announcement_date": announced.isoformat(),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "occurrence_count": integer(raw[at["累計"]]),
            "reason": str(raw[at["處置原因"]]).strip(),
            "measure": str(raw[at["處置內容"]]).strip(),
            "close": number(raw[at["收盤價"]]),
            "pe_ratio": number(raw[at["本益比"]]),
            "market": "TPEX",
        })
    return pd.DataFrame(rows, columns=columns)


RESTRICTION_COLUMNS = [
    "snapshot_date", "stock_id", "stock_name", "altered_trading",
    "periodic_trading", "managed_stock", "match_interval_minutes", "suspended",
    "financial_focus", "source_detail", "market",
]


def _yes(value: object) -> bool:
    return str(value or "").strip().upper() in {"Y", "Ｙ", "YES", "是"}


def parse_trading_restrictions(payload: dict, snapshot_date: date) -> pd.DataFrame:
    """Parse the modern daily altered/periodic/managed/suspended table."""
    table = _table(payload, "變更交易")
    if table is None:
        return pd.DataFrame(columns=RESTRICTION_COLUMNS)
    fields = [str(field).strip() for field in table.get("fields") or []]
    required = [
        "證券代號", "證券名稱", "變更交易", "分盤交易", "屬管理股票",
        "分盤或管理股票撮合循環時間(分鐘)", "停止交易", "財務資訊重點專區",
    ]
    missing = [field for field in required if field not in fields]
    if missing:
        raise ValueError(f"TPEx altered trading table missing fields: {missing}")
    at = {field: fields.index(field) for field in required}
    rows = []
    for raw in table.get("data") or []:
        stock_id = str(raw[at["證券代號"]]).strip()
        if not re.fullmatch(r"\d{4}", stock_id):
            continue
        interval = integer(raw[at["分盤或管理股票撮合循環時間(分鐘)"]])
        rows.append({
            "snapshot_date": snapshot_date.isoformat(),
            "stock_id": stock_id,
            "stock_name": str(raw[at["證券名稱"]]).strip(),
            "altered_trading": _yes(raw[at["變更交易"]]),
            "periodic_trading": _yes(raw[at["分盤交易"]]),
            "managed_stock": _yes(raw[at["屬管理股票"]]),
            "match_interval_minutes": interval,
            "suspended": _yes(raw[at["停止交易"]]),
            "financial_focus": _yes(raw[at["財務資訊重點專區"]]),
            "source_detail": "TPEX_afterTrading_chtm_modern_full_flags",
            "market": "TPEX",
        })
    return pd.DataFrame(rows, columns=RESTRICTION_COLUMNS)


def parse_legacy_altered_html(raw: bytes, snapshot_date: date) -> pd.DataFrame:
    """Parse the official legacy table (only altered-trading membership exists)."""
    text = raw.decode("big5", errors="replace")
    # Each data row has a four-digit code in the first TD and the name in the next.
    pairs = re.findall(
        r"<TR[^>]*>\s*<TD[^>]*>\s*(\d{4})\s*</TD>\s*"
        r"<TD[^>]*>\s*([^<]*?)\s*</TD>",
        text,
        flags=re.IGNORECASE,
    )
    rows = [{
        "snapshot_date": snapshot_date.isoformat(),
        "stock_id": stock_id,
        "stock_name": name.strip().replace("�", ""),
        "altered_trading": True,
        "periodic_trading": pd.NA,
        "managed_stock": pd.NA,
        "match_interval_minutes": pd.NA,
        "suspended": pd.NA,
        "financial_focus": pd.NA,
        "source_detail": "TPEX_hist_CHTM_legacy_altered_only_other_flags_unknown",
        "market": "TPEX",
    } for stock_id, name in pairs]
    return pd.DataFrame(rows, columns=RESTRICTION_COLUMNS)
