"""
data_pipeline/fetchers/corporate_actions_fetcher.py

除權息事件 + 下市股票清單（SPEC_QUANT_UPGRADE.md P0-2：個股還原價引擎的資料來源）。

沒有這兩份資料，回測有兩個真實的偏誤：
  1. 除權息日的價格假跳空（可達 -8%）會誤觸出場規則（跌破實體底/停損等），
     台股除權息旺季（7~9月）整批持倉被錯殺，且股息完全沒算進報酬（保守方向的
     偏差，但會扭曲出場規則消融的結論）；
  2. 用「今天還活著的股票」回測過去，過去下市的地雷股不在樣本裡＝倖存者偏誤，
     對 10 年窗的報酬高估影響遠大於現行 13 個月窗。

來源（皆為官方端點，免 key）：
  TWSE 除權息：https://www.twse.com.tw/exchangeReport/TWT49U（startDate/endDate，
               只給「權值+息值」合計，不分現金/股票股利細項——夠算還原價）
  TPEX  除權息：https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ（startDate/endDate，
               有分「權值」「息值」兩欄，比 TWSE 詳細）
  TWSE 下市清單：https://openapi.twse.com.tw/v1/company/suspendListingCsvAndHtml
  TPEX 下市清單：**尚未找到對應端點**（已測試 tpex_delisted_stock/tpex_suspend_listing
               皆為 404），暫缺——回測時上櫃股票仍只用現存清單，這是已知誠實缺口。

用法：
  backfill_dividend_events(start_year=2015)   — 逐月回補兩市場除權息事件
  backfill_delisted_stocks()                  — 回補 TWSE 下市清單（TPEX 暫缺）
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import re
import time
from datetime import date

import pandas as pd
import requests
from loguru import logger
from sqlalchemy import text

from database.connection import get_session

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_TIMEOUT = 30

URL_TWSE_DIVIDEND = "https://www.twse.com.tw/exchangeReport/TWT49U"
URL_TPEX_DIVIDEND = "https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ"
URL_TWSE_DELISTED = "https://openapi.twse.com.tw/v1/company/suspendListingCsvAndHtml"


def ensure_corporate_actions_tables():
    """現有 DB（init.sql 不會重跑）也能自動建立這兩張表，冪等。"""
    with get_session() as s:
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS dividend_events (
                id              BIGSERIAL    PRIMARY KEY,
                stock_id        VARCHAR(10)  NOT NULL REFERENCES stocks(stock_id),
                ex_date         DATE         NOT NULL,
                pre_close       NUMERIC(12,4),
                ref_price       NUMERIC(12,4),
                cash_dividend   NUMERIC(12,4) DEFAULT 0,
                stock_dividend_ratio NUMERIC(12,6) DEFAULT 0,
                event_type      VARCHAR(10)  NOT NULL DEFAULT '除息'
                                    CHECK (event_type IN ('除息', '除權', '除權息')),
                market          VARCHAR(10)  NOT NULL CHECK (market IN ('TWSE', 'TPEX')),
                created_at      TIMESTAMPTZ  DEFAULT now(),
                UNIQUE (stock_id, ex_date)
            )
        """))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_dividend_events_stock ON dividend_events (stock_id, ex_date)"))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_dividend_events_date   ON dividend_events (ex_date)"))
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS delisted_stocks (
                stock_id        VARCHAR(10)  PRIMARY KEY,
                stock_name      VARCHAR(50),
                delisting_date  DATE,
                market          VARCHAR(10)  CHECK (market IN ('TWSE', 'TPEX')),
                created_at      TIMESTAMPTZ  DEFAULT now()
            )
        """))


def _roc_to_date(s: str) -> date | None:
    """'113年07月01日' 或 '113/07/01' → date(2024,7,1)。"""
    s = str(s).strip()
    m = re.match(r"(\d{2,3})[年/](\d{1,2})[月/](\d{1,2})日?", s)
    if not m:
        return None
    y = int(m.group(1)) + 1911
    try:
        return date(y, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _num(v) -> float | None:
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if not s or s in ("-", "--", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


_EVENT_TYPE_MAP = {"息": "除息", "權": "除權", "除權息": "除權息", "除息": "除息", "除權": "除權"}


def fetch_dividend_events_twse(start: date, end: date) -> pd.DataFrame:
    """TWSE 除權息，startDate/endDate 為 YYYYMMDD（西元）。單次呼叫涵蓋整個區間。"""
    try:
        r = requests.get(URL_TWSE_DIVIDEND, headers=_UA, timeout=_TIMEOUT, params={
            "response": "json",
            "startDate": start.strftime("%Y%m%d"),
            "endDate": end.strftime("%Y%m%d"),
        })
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        logger.warning(f"  TWSE除權息 {start}~{end} 抓取失敗: {e}")
        return pd.DataFrame()
    if j.get("stat") != "OK" or not j.get("data"):
        return pd.DataFrame()

    rows = []
    for row in j["data"]:
        ex_date = _roc_to_date(row[0])
        sid = str(row[1]).strip()
        if not ex_date or not re.fullmatch(r"\d{4}", sid):   # 排除特別股/權證等非普通股代號
            continue
        pre_close, ref_price = _num(row[3]), _num(row[4])
        raw_type = str(row[6]).strip() if len(row) > 6 else ""
        event_type = _EVENT_TYPE_MAP.get(raw_type, "除權息")
        cash_dividend = (pre_close - ref_price) if event_type == "除息" and pre_close and ref_price else None
        rows.append({
            "stock_id": sid, "ex_date": ex_date, "pre_close": pre_close, "ref_price": ref_price,
            "cash_dividend": cash_dividend, "stock_dividend_ratio": None,
            "event_type": event_type, "market": "TWSE",
        })
    return pd.DataFrame(rows)


def fetch_dividend_events_tpex(start: date, end: date) -> pd.DataFrame:
    """TPEX 除權息，startDate/endDate 為 YYYY/MM/DD。分「權值」「息值」兩欄，比TWSE詳細。"""
    try:
        r = requests.get(URL_TPEX_DIVIDEND, headers=_UA, timeout=_TIMEOUT, params={
            "response": "json",
            "startDate": start.strftime("%Y/%m/%d"),
            "endDate": end.strftime("%Y/%m/%d"),
        })
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        logger.warning(f"  TPEX除權息 {start}~{end} 抓取失敗: {e}")
        return pd.DataFrame()
    tables = j.get("tables") or []
    if str(j.get("stat", "")).lower() != "ok" or not tables or not tables[0].get("data"):
        return pd.DataFrame()

    rows = []
    for row in tables[0]["data"]:
        ex_date = _roc_to_date(row[0])
        sid = str(row[1]).strip()
        if not ex_date or not re.fullmatch(r"\d{4}", sid):
            continue
        raw_type = str(row[8]).strip() if len(row) > 8 else ""
        event_type = _EVENT_TYPE_MAP.get(raw_type.replace("除", ""), _EVENT_TYPE_MAP.get(raw_type, "除權息"))
        rows.append({
            "stock_id": sid, "ex_date": ex_date,
            "pre_close": _num(row[3]), "ref_price": _num(row[4]),
            "stock_dividend_ratio": _num(row[5]), "cash_dividend": _num(row[6]),
            "event_type": event_type, "market": "TPEX",
        })
    return pd.DataFrame(rows)


def upsert_dividend_events(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    with get_session() as s:
        known = {r[0] for r in s.execute(text("SELECT stock_id FROM stocks")).fetchall()}
        df = df[df["stock_id"].isin(known)]
        if df.empty:
            return 0
        s.execute(text("""
            INSERT INTO dividend_events
                (stock_id, ex_date, pre_close, ref_price, cash_dividend,
                 stock_dividend_ratio, event_type, market)
            VALUES
                (:stock_id, :ex_date, :pre_close, :ref_price, :cash_dividend,
                 :stock_dividend_ratio, :event_type, :market)
            ON CONFLICT (stock_id, ex_date) DO UPDATE SET
                pre_close = EXCLUDED.pre_close, ref_price = EXCLUDED.ref_price,
                cash_dividend = EXCLUDED.cash_dividend,
                stock_dividend_ratio = EXCLUDED.stock_dividend_ratio,
                event_type = EXCLUDED.event_type
        """), df.to_dict("records"))
    return len(df)


def _month_ranges(start_year: int, end: date | None = None):
    """逐月切區間（避免單次請求範圍過大被官方端點拒絕，已知安全上限是月級）。"""
    from datetime import timedelta
    from calendar import monthrange
    end = end or date.today()
    y, m = start_year, 1
    while date(y, m, 1) <= end:
        last_day = monthrange(y, m)[1]
        chunk_end = min(date(y, m, last_day), end)
        yield date(y, m, 1), chunk_end
        m += 1
        if m > 12:
            m, y = 1, y + 1


def backfill_dividend_events(start_year: int = 2015, delay: float = 1.0) -> int:
    """逐月回補兩市場除權息事件。回傳寫入筆數。可重跑（ON CONFLICT upsert，不重複）。"""
    ensure_corporate_actions_tables()
    total = 0
    for start, end in _month_ranges(start_year):
        twse = fetch_dividend_events_twse(start, end)
        time.sleep(delay)
        tpex = fetch_dividend_events_tpex(start, end)
        time.sleep(delay)
        n = upsert_dividend_events(pd.concat([twse, tpex], ignore_index=True))
        total += n
        logger.info(f"  除權息 {start}~{end}：寫入 {n} 筆（累計 {total}）")
    logger.info(f"=== 除權息回補完成：共 {total} 筆 ===")
    return total


def backfill_delisted_stocks() -> int:
    """
    回補 TWSE 下市清單（TPEX 暫缺，見檔案開頭說明），**同時把這些股票登記進
    `stocks` 表**（is_active=False）。這步是必要的，不是順手做：`daily_prices`／
    `dividend_events` 對 stock_id 都有外鍵約束到 stocks 表，pipeline 各處的
    `_filter_known()` 也只認「stocks 表裡已存在」的代號——不先登記，等下歷史
    回補時這些下市股的價量資料會被整批靜默濾掉，倖存者偏誤依舊修不掉。
    """
    ensure_corporate_actions_tables()
    try:
        r = requests.get(URL_TWSE_DELISTED, headers=_UA, timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error(f"TWSE下市清單抓取失敗: {e}")
        return 0

    rows = []
    for d in data:
        sid = str(d.get("Code", "")).strip()
        if not re.fullmatch(r"\d{4}", sid):
            continue
        rows.append({
            "stock_id": sid, "stock_name": (d.get("Company") or "").strip(),
            "delisting_date": _parse_roc_slash(d.get("DelistingDate")),
            "market": "TWSE",
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return 0
    with get_session() as s:
        s.execute(text("""
            INSERT INTO stocks (stock_id, stock_name, market, is_active)
            VALUES (:stock_id, :stock_name, :market, FALSE)
            ON CONFLICT (stock_id) DO NOTHING
        """), df.to_dict("records"))
        s.execute(text("""
            INSERT INTO delisted_stocks (stock_id, stock_name, delisting_date, market)
            VALUES (:stock_id, :stock_name, :delisting_date, :market)
            ON CONFLICT (stock_id) DO UPDATE SET
                delisting_date = EXCLUDED.delisting_date, stock_name = EXCLUDED.stock_name
        """), df.to_dict("records"))
    logger.info(f"=== TWSE下市清單回補完成：{len(df)} 家（已登記進 stocks 表 is_active=False）===")
    return len(df)


def _parse_roc_slash(s: str) -> date | None:
    """'115/06/23' → date(2026,6,23)（TWSE下市清單日期用斜線分隔的民國年）。"""
    if not s:
        return None
    m = re.match(r"(\d{2,3})/(\d{1,2})/(\d{1,2})", str(s).strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dividends-from", type=int, default=None, help="回補除權息，從此西元年開始")
    ap.add_argument("--delisted", action="store_true", help="回補下市清單")
    a = ap.parse_args()
    if a.dividends_from:
        backfill_dividend_events(a.dividends_from)
    if a.delisted:
        backfill_delisted_stocks()
