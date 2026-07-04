"""
data_pipeline/fetchers/us_market.py

美股收盤速覽（免 API key，資料來源 Yahoo Finance chart API；
stooq 已改為需要 JS 驗證，2026-07 起不可用）。
台灣 21:00 跑 pipeline 時，抓「前一晚」美股收盤：
  S&P500 / NASDAQ / 費城半導體 / 台積電 ADR

結果寫一筆 market_signals（signal_type='news', source='美股指數'），
自然出現在 市場情報→財經新聞 頁與每日彙整的輸入中。
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from datetime import date, datetime

import requests
from loguru import logger
from sqlalchemy import text

from database.connection import get_session
from config.settings import tw_today

# Yahoo 代碼 → 顯示名稱
SYMBOLS = [
    ("^GSPC", "S&P500"),
    ("^IXIC", "NASDAQ"),
    ("^SOX",  "費城半導體"),
    ("TSM",   "台積電ADR"),
]

_YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _fetch_change(sym: str) -> tuple[float, float, str] | None:
    """回傳 (最新收盤, 漲跌%, 收盤日期字串)；失敗回 None。"""
    try:
        r = requests.get(_YAHOO_URL.format(sym=sym), headers=HEADERS, timeout=20)
        r.raise_for_status()
        result = r.json()["chart"]["result"][0]
        ts     = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
        # 去掉 None（假日/未收盤）後取最後兩根
        pairs = [(t, c) for t, c in zip(ts, closes) if c is not None]
        if len(pairs) < 2:
            return None
        (_, c_prev), (t_last, c_last) = pairs[-2], pairs[-1]
        chg = (c_last / c_prev - 1) * 100
        dstr = datetime.fromtimestamp(t_last).strftime("%m/%d")
        return c_last, chg, dstr
    except Exception as e:
        logger.warning(f"  Yahoo {sym} 抓取失敗: {e}")
        return None


def fetch_us_market_summary() -> int:
    """抓美股收盤寫入 market_signals。當日已有記錄則跳過。回傳寫入筆數。"""
    today = tw_today()

    with get_session() as s:
        exists = s.execute(text("""
            SELECT 1 FROM market_signals
            WHERE source = '美股指數' AND signal_date = :dt LIMIT 1
        """), {"dt": today}).fetchone()
    if exists:
        logger.info("  美股速覽：今日已存在，跳過")
        return 0

    parts, us_date, spx_chg = [], None, None
    for sym, label in SYMBOLS:
        r = _fetch_change(sym)
        if r is None:
            continue
        close, chg, dstr = r
        parts.append(f"{label} {chg:+.2f}%")
        if us_date is None:
            us_date = dstr
        if sym == "^GSPC":
            spx_chg = chg

    if not parts:
        logger.warning("  美股速覽：全部來源失敗")
        return 0

    sentiment = ("positive" if (spx_chg or 0) > 0.3
                 else "negative" if (spx_chg or 0) < -0.3
                 else "neutral")
    title = f"美股收盤速覽（{us_date}）"
    summary = "｜".join(parts)

    with get_session() as s:
        s.execute(text("""
            INSERT INTO market_signals
                (signal_type, source, title, summary, sentiment, signal_date)
            VALUES ('news', '美股指數', :t, :su, :se, :dt)
            ON CONFLICT DO NOTHING
        """), {"t": title, "su": summary, "se": sentiment, "dt": today})

    logger.info(f"  美股速覽：{summary}")
    return 1
