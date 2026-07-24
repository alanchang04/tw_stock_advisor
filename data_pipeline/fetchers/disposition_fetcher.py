"""
data_pipeline/fetchers/disposition_fetcher.py

處置股 / 注意股清單（SPEC_QUANT_UPGRADE.md §2.4，P0 資料地基最後一項）。

**為什麼需要**：SPEC_STRATEGY_MIDCAP §2.1 的硬門檻表裡「排除警示」一直掛著
「(待補，見資料缺口)」，程式碼中**完全沒有**任何處置股過濾邏輯。後果：

  1. **回測不寫實**：處置期間是「人工管制撮合（約每五分鐘一次）」+ 委託量達
     十交易單位須預收款券。回測假設隔日開盤照常成交、照 30bp 滑價算，
     對處置股而言是**嚴重低估交易成本**；
  2. **即時選股會真的推薦到**：成交金額門檻擋掉大部分低流動性阿呆股，但處置
     常常是「爆量急漲」觸發的——那正是動能策略最愛選的形態，門檻擋不住。

**資料源**（官方，免 key，實測可回溯至 2015）：
  處置：https://www.twse.com.tw/rwd/zh/announcement/punish
        ?response=json&startDate=YYYYMMDD&endDate=YYYYMMDD
        欄位：編號, 公布日期, 證券代號, 證券名稱, 累計, 處置條件,
              處置起迄時間, 處置措施, 處置內容, 備註
  注意：https://www.twse.com.tw/rwd/zh/announcement/notice（同樣 startDate/endDate）
        欄位：編號, 證券代號, 證券名稱, 累計次數, 注意交易資訊, 日期, 收盤價, 本益比

**兩個刻意的取捨（誠實記錄）**：
  - **只留 4 位數代號**。原始資料 60% 以上是權證（6 位數），權證不在候選池裡
    （不在 `stocks` 表、也過不了價格/流動性門檻），存進來只是灌大表。
  - **只有上市（TWSE）**。上櫃 www.tpex.org.tw 憑證缺 Subject Key Identifier，
    Python 3.14 嚴格驗證會拒絕（同 margin_fetcher 的已知限制）。這是誠實缺口：
    上櫃處置股目前擋不掉。

**注意股（notice）目前只存不用**：§2.4 只要求「排除處置股」。注意股一年 3000+ 筆、
涵蓋相當大比例的活躍股，直接全排會過度殺傷；先留資料，日後要當因子再用
（且必須先過 P1 的 IC 檢驗，不是憑直覺加——見 §5.5 券資比的教訓）。

實測筆數（4 位數個股，處置事件）：11 年合計 1,556 筆，2024 年 163 筆／113 檔。
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

URL_TWSE_PUNISH = "https://www.twse.com.tw/rwd/zh/announcement/punish"
URL_TWSE_NOTICE = "https://www.twse.com.tw/rwd/zh/announcement/notice"

#: 只收 4 位數個股代號（權證/ETN 等 6 位數不在候選池，見模組說明）
_STOCK_ID = re.compile(r"^\d{4}$")
#: 民國日期：113/10/07 或 113.10.07，公布日期偶爾前綴 '*'（表示更正過）
_ROC_DATE = re.compile(r"^\*?\s*(\d{2,3})[/.](\d{1,2})[/.](\d{1,2})\s*$")


def parse_roc_date(s) -> date | None:
    """民國日期字串 → date。'113/10/07'、'113.10.07'、'*115/07/24' 都吃得下。"""
    if s is None:
        return None
    m = _ROC_DATE.match(str(s))
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        return date(y + 1911, mo, d)
    except ValueError:
        return None


def parse_roc_period(s) -> tuple[date | None, date | None]:
    """'113/10/07～113/10/21' → (date, date)。分隔符可能是全形～或半形~。"""
    if s is None:
        return None, None
    parts = re.split(r"[～~]", str(s))
    if len(parts) != 2:
        return None, None
    return parse_roc_date(parts[0]), parse_roc_date(parts[1])


def parse_punish_rows(rows: list) -> pd.DataFrame:
    """TWSE punish 的 data 陣列 → 標準 DataFrame（純函式，好測）。

    只留 4 位數個股，且處置起迄兩端都解析得出來的列——起迄是這張表的用途所在
    （判斷某一天該股是否處於處置期間），解不出來的列留著也沒用。
    """
    out = []
    for r in rows or []:
        if len(r) < 8:
            continue
        sid = str(r[2]).strip()
        if not _STOCK_ID.match(sid):
            continue
        start, end = parse_roc_period(r[6])
        if start is None or end is None:
            continue
        try:
            cum = int(str(r[4]).strip())
        except (ValueError, TypeError):
            cum = None
        out.append({
            "stock_id": sid,
            "announce_date": parse_roc_date(r[1]),
            "start_date": start,
            "end_date": end,
            "cumulative": cum,
            "reason": str(r[5]).strip()[:100] if r[5] is not None else None,
            "measure": str(r[7]).strip()[:50] if r[7] is not None else None,
            "market": "TWSE",
        })
    df = pd.DataFrame(out)
    if not df.empty:
        # 同一檔同一段處置期間可能因更正而重覆公布，以 (代號, 起日) 去重、留最後一筆
        df = df.drop_duplicates(subset=["stock_id", "start_date"], keep="last")
    return df


def parse_notice_rows(rows: list) -> pd.DataFrame:
    """TWSE notice 的 data 陣列 → 標準 DataFrame。注意股是「當日」事件，沒有期間。"""
    out = []
    for r in rows or []:
        if len(r) < 6:
            continue
        sid = str(r[1]).strip()
        if not _STOCK_ID.match(sid):
            continue
        d = parse_roc_date(r[5])
        if d is None:
            continue
        out.append({
            "stock_id": sid,
            "notice_date": d,
            "reason": str(r[4]).strip()[:200] if r[4] is not None else None,
            "market": "TWSE",
        })
    df = pd.DataFrame(out)
    if not df.empty:
        df = df.drop_duplicates(subset=["stock_id", "notice_date", "reason"], keep="last")
    return df


def _get(url: str, start: date, end: date) -> list:
    r = requests.get(url, params={"response": "json",
                                  "startDate": start.strftime("%Y%m%d"),
                                  "endDate": end.strftime("%Y%m%d")},
                     headers=_UA, timeout=_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if j.get("stat") != "OK":
        logger.warning(f"TWSE 回應非 OK：{j.get('stat')}")
        return []
    return j.get("data") or []


def fetch_punish(start: date, end: date) -> pd.DataFrame:
    """抓一段期間的處置公告。TWSE 接受跨年區間，一次一年是穩妥的粒度。"""
    return parse_punish_rows(_get(URL_TWSE_PUNISH, start, end))


def fetch_notice(start: date, end: date) -> pd.DataFrame:
    return parse_notice_rows(_get(URL_TWSE_NOTICE, start, end))


def ensure_disposition_tables():
    """冪等建表——現有 DB 不會重跑 init.sql，比照 corporate_actions_fetcher 的做法。"""
    with get_session() as s:
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS disposition_events (
                id            BIGSERIAL   PRIMARY KEY,
                stock_id      VARCHAR(10) NOT NULL,
                announce_date DATE,
                start_date    DATE        NOT NULL,
                end_date      DATE        NOT NULL,
                cumulative    INTEGER,
                reason        VARCHAR(100),
                measure       VARCHAR(50),
                market        VARCHAR(10) NOT NULL DEFAULT 'TWSE',
                created_at    TIMESTAMPTZ DEFAULT now(),
                UNIQUE (stock_id, start_date)
            )
        """))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_disposition_stock "
                       "ON disposition_events (stock_id, start_date, end_date)"))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_disposition_period "
                       "ON disposition_events (start_date, end_date)"))
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS notice_events (
                id          BIGSERIAL   PRIMARY KEY,
                stock_id    VARCHAR(10) NOT NULL,
                notice_date DATE        NOT NULL,
                reason      VARCHAR(200),
                market      VARCHAR(10) NOT NULL DEFAULT 'TWSE',
                created_at  TIMESTAMPTZ DEFAULT now(),
                UNIQUE (stock_id, notice_date, reason)
            )
        """))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_notice_stock "
                       "ON notice_events (stock_id, notice_date)"))
        s.commit()
    logger.info("disposition_events / notice_events 表已就緒")


def _upsert(df: pd.DataFrame, table: str, conflict_cols: str) -> int:
    """批次 upsert（executemany）——比照 finmind_fetcher，避免逐列往返 Neon。"""
    if df.empty:
        return 0
    cols = list(df.columns)
    stmt = text(f"INSERT INTO {table} ({', '.join(cols)}) "
                f"VALUES ({', '.join(':' + c for c in cols)}) "
                f"ON CONFLICT {conflict_cols} DO NOTHING")
    recs = df.astype(object).where(pd.notnull(df), None).to_dict("records")
    with get_session() as s:
        s.execute(stmt, recs)
        s.commit()
    return len(recs)


def backfill_disposition(start_year: int = 2015, end_year: int | None = None,
                         include_notice: bool = True) -> dict:
    """逐年回補處置 / 注意公告。回傳 {'disposition': n, 'notice': n}。"""
    ensure_disposition_tables()
    end_year = end_year or date.today().year
    tot = {"disposition": 0, "notice": 0}
    for y in range(start_year, end_year + 1):
        lo, hi = date(y, 1, 1), date(y, 12, 31)
        try:
            d = fetch_punish(lo, hi)
            tot["disposition"] += _upsert(d, "disposition_events",
                                          "(stock_id, start_date)")
            logger.info(f"{y} 處置：{len(d)} 筆")
        except Exception as e:
            logger.error(f"{y} 處置回補失敗：{e}")
        time.sleep(1)
        if include_notice:
            try:
                n = fetch_notice(lo, hi)
                tot["notice"] += _upsert(n, "notice_events",
                                         "(stock_id, notice_date, reason)")
                logger.info(f"{y} 注意：{len(n)} 筆")
            except Exception as e:
                logger.error(f"{y} 注意回補失敗：{e}")
            time.sleep(1)
    logger.info(f"=== 回補完成：處置 {tot['disposition']} 筆、注意 {tot['notice']} 筆 ===")
    return tot


def load_disposition_ranges() -> pd.DataFrame:
    """讀出所有處置期間，供選股/回測過濾用。欄位：stock_id, start_date, end_date。"""
    with get_session() as s:
        rows = s.execute(text(
            "SELECT stock_id, start_date, end_date FROM disposition_events"
        )).fetchall()
    return pd.DataFrame(rows, columns=["stock_id", "start_date", "end_date"])


if __name__ == "__main__":
    backfill_disposition()
