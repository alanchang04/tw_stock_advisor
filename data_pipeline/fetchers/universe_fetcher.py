"""Official listing-date backfill and point-in-time universe snapshots."""

from __future__ import annotations

import re
from datetime import date

import pandas as pd
import requests
from loguru import logger
from sqlalchemy import text

from database.connection import get_session


URL_TWSE_COMPANIES = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
URL_TPEX_COMPANIES = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _parse_listing_date(value) -> date | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    parts = [int(x) for x in re.findall(r"\d+", raw)]
    try:
        if len(parts) == 1 and len(raw) == 8:
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        if len(parts) >= 3:
            year, month, day = parts[:3]
            if year < 1911:
                year += 1911
            return date(year, month, day)
    except ValueError:
        return None
    return None


def _first(row: dict, *keys):
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _parse_company_rows(rows: list[dict], market: str) -> pd.DataFrame:
    parsed = []
    for row in rows:
        sid = str(_first(
            row, "公司代號", "公司代碼", "SecuritiesCompanyCode", "Code"
        ) or "").strip()
        if not re.fullmatch(r"\d{4}", sid):
            continue
        listing_value = _first(
            row, "上市日期", "上櫃日期", "掛牌日期", "DateOfListing"
        )
        parsed.append({
            "stock_id": sid,
            "stock_name": str(_first(
                row, "公司簡稱", "公司名稱", "CompanyName", "Name"
            ) or sid).strip(),
            "market": market,
            "listing_date": _parse_listing_date(listing_value),
            "asset_type": "common_stock",
        })
    return pd.DataFrame(parsed)


def fetch_listing_metadata(timeout: int = 60) -> pd.DataFrame:
    frames = []
    for market, url in (
        ("TWSE", URL_TWSE_COMPANIES),
        ("TPEX", URL_TPEX_COMPANIES),
    ):
        response = requests.get(url, headers=_HEADERS, timeout=timeout)
        response.raise_for_status()
        frame = _parse_company_rows(response.json(), market)
        logger.info(f"{market} company metadata: {len(frame)} rows")
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=[
            "stock_id", "stock_name", "market", "listing_date", "asset_type"
        ])
    return pd.concat(frames, ignore_index=True).drop_duplicates("stock_id")


def ensure_universe_history_table() -> None:
    with get_session() as session:
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS stock_universe_history (
                snapshot_date DATE NOT NULL,
                stock_id VARCHAR(20) NOT NULL REFERENCES stocks(stock_id),
                market VARCHAR(10) NOT NULL,
                industry_code VARCHAR(20),
                asset_type VARCHAR(20) NOT NULL DEFAULT 'common_stock',
                listing_date DATE,
                delisting_date DATE,
                is_active BOOLEAN NOT NULL,
                PRIMARY KEY (snapshot_date, stock_id)
            )
        """))
        session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_universe_history_stock_date
            ON stock_universe_history (stock_id, snapshot_date DESC)
        """))


def backfill_listing_dates(frame: pd.DataFrame | None = None) -> int:
    frame = fetch_listing_metadata() if frame is None else frame
    if frame.empty:
        return 0
    records = frame.dropna(subset=["listing_date"]).to_dict("records")
    with get_session() as session:
        result = session.execute(text("""
            UPDATE stocks AS s
            SET listing_date = v.listing_date,
                market = v.market,
                stock_name = COALESCE(NULLIF(v.stock_name, ''), s.stock_name),
                updated_at = NOW()
            FROM (VALUES (:stock_id, :stock_name, :market, :listing_date))
                 AS v(stock_id, stock_name, market, listing_date)
            WHERE s.stock_id = v.stock_id
              AND (s.listing_date IS NULL OR s.listing_date <> v.listing_date)
        """), records)
    return max(0, result.rowcount or 0)


def snapshot_stock_universe(snapshot_date: date | None = None) -> int:
    ensure_universe_history_table()
    as_of = snapshot_date or date.today()
    with get_session() as session:
        result = session.execute(text("""
            INSERT INTO stock_universe_history
                (snapshot_date, stock_id, market, industry_code, asset_type,
                 listing_date, delisting_date, is_active)
            SELECT :snapshot_date, s.stock_id, s.market, m.industry_code,
                   'common_stock', s.listing_date, d.delisting_date,
                   CASE
                     WHEN s.listing_date IS NOT NULL AND s.listing_date > :snapshot_date
                       THEN FALSE
                     WHEN d.delisting_date IS NOT NULL AND d.delisting_date < :snapshot_date
                       THEN FALSE
                     ELSE COALESCE(s.is_active, TRUE)
                   END
            FROM stocks s
            LEFT JOIN LATERAL (
                SELECT industry_code
                FROM stock_industry_map
                WHERE stock_id = s.stock_id
                ORDER BY industry_code
                LIMIT 1
            ) m ON TRUE
            LEFT JOIN delisted_stocks d ON d.stock_id = s.stock_id
            WHERE s.stock_id ~ '^[0-9]{4}$'
            ON CONFLICT (snapshot_date, stock_id) DO UPDATE SET
                market = EXCLUDED.market,
                industry_code = EXCLUDED.industry_code,
                asset_type = EXCLUDED.asset_type,
                listing_date = EXCLUDED.listing_date,
                delisting_date = EXCLUDED.delisting_date,
                is_active = EXCLUDED.is_active
        """), {"snapshot_date": as_of})
    return max(0, result.rowcount or 0)
