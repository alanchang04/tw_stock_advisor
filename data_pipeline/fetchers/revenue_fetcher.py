"""
data_pipeline/fetchers/revenue_fetcher.py

月營收擷取（財報因子基礎，FinLab 式營收動能的資料層）。

來源：MOPS 彙總頁（免 API key、全市場一次抓完，上市+上櫃各 1 請求）
  https://mopsov.twse.com.tw/nas/t21/{sii|otc}/t21sc03_{民國年}_{月}_0.html
欄位：代號 名稱 當月營收 上月營收 去年當月營收 上月增減% 去年同月增減% ...
公布時程：每月 10 日前公布上個月營收。

用法：
  run_revenue_fetch()            — 抓最新已公布月份（已存在則跳過，每日 pipeline 用）
  backfill_revenue(months=24)    — 回補歷史（首次執行）
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import time
from datetime import date

import requests
from bs4 import BeautifulSoup
from loguru import logger
from sqlalchemy import text

from database.connection import get_session
from config.settings import tw_today

_URL = "https://mopsov.twse.com.tw/nas/t21/{market}/t21sc03_{roc}_{month}_0.html"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _num(s: str) -> float | None:
    s = s.replace(",", "").strip()
    if not s or s in ("-", "N/A", "不適用"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def latest_published_month(today: date = None) -> tuple[int, int]:
    """最新「應已公布」的營收月份：每月 10 日後為上月，否則上上月。"""
    if today is None:
        today = tw_today()
    y, m = today.year, today.month - (1 if today.day > 10 else 2)
    while m <= 0:
        m += 12
        y -= 1
    return y, m


def fetch_month(year: int, month: int) -> int:
    """抓某年月（西元）的上市+上櫃月營收，upsert 進 monthly_revenue。回傳筆數。"""
    roc = year - 1911
    ym = f"{year:04d}-{month:02d}"
    rows_all = []

    for market in ("sii", "otc"):
        url = _URL.format(market=market, roc=roc, month=month)
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code != 200:
                logger.warning(f"  {ym} {market}: HTTP {r.status_code}（可能尚未公布）")
                continue
            r.encoding = "big5"
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            logger.warning(f"  {ym} {market} 抓取失敗: {e}")
            continue

        for tr in soup.find_all("tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            # 公司列：代號為 4 碼數字；欄位至少到「去年同月增減(%)」
            if len(tds) >= 7 and tds[0].isdigit() and len(tds[0]) == 4:
                rev = _num(tds[2])
                rows_all.append({
                    "sid": tds[0], "ym": ym,
                    "rev": int(rev) if rev is not None else None,
                    "mom": _num(tds[5]),
                    "yoy": _num(tds[6]),
                })

    if not rows_all:
        return 0

    with get_session() as s:
        s.execute(text("""
            INSERT INTO monthly_revenue (stock_id, year_month, revenue, mom_pct, yoy_pct)
            VALUES (:sid, :ym, :rev, :mom, :yoy)
            ON CONFLICT (stock_id, year_month) DO UPDATE SET
                revenue = EXCLUDED.revenue,
                mom_pct = EXCLUDED.mom_pct,
                yoy_pct = EXCLUDED.yoy_pct
        """), rows_all)

    logger.info(f"  月營收 {ym}：寫入 {len(rows_all)} 家")
    return len(rows_all)


def run_revenue_fetch() -> int:
    """每日 pipeline 用：抓最新已公布月份，已有資料（>500 家）就跳過。"""
    y, m = latest_published_month()
    ym = f"{y:04d}-{m:02d}"
    with get_session() as s:
        cnt = s.execute(text(
            "SELECT COUNT(*) FROM monthly_revenue WHERE year_month = :ym"
        ), {"ym": ym}).scalar()
    if cnt and cnt > 500:
        logger.info(f"  月營收 {ym} 已有 {cnt} 家，跳過")
        return 0
    logger.info(f"=== 抓取月營收 {ym} ===")
    return fetch_month(y, m)


def backfill_revenue(months: int = 24, delay: float = 1.0) -> int:
    """回補最近 N 個月（首次執行用）。"""
    y, m = latest_published_month()
    total = 0
    for _ in range(months):
        total += fetch_month(y, m)
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        time.sleep(delay)
    logger.info(f"=== 月營收回補完成：共 {total} 筆 ===")
    return total


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0, help="回補幾個月")
    a = ap.parse_args()
    if a.backfill:
        backfill_revenue(a.backfill)
    else:
        run_revenue_fetch()
