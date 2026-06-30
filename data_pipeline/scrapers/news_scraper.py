"""
data_pipeline/scrapers/news_scraper.py

財經新聞爬蟲
  來源 1：鉅亨網 RSS（台股新聞）
  來源 2：MOPS 重大訊息（公開資訊觀測站）
  結果寫入 market_signals 表
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import xml.etree.ElementTree as ET
from datetime import date, datetime
import re
import requests
from loguru import logger
from sqlalchemy import text

from database.connection import get_session


# ── RSS 來源設定 ─────────────────────────────────────────────────
# 要加新來源：直接在列表裡新增一個 dict 即可（region: "tw"=台股、"us"=美股）
RSS_SOURCES = [
    # ── 台灣財經（Google News RSS，穩定不需 API key）────────────
    {
        "name":   "台股新聞",
        "url":    "https://news.google.com/rss/search?q=台股+利多+利空&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "region": "tw",
    },
    {
        "name":   "台股半導體",
        "url":    "https://news.google.com/rss/search?q=台灣+半導體+電子股&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "region": "tw",
    },
    {
        "name":   "ETF新聞",
        "url":    "https://news.google.com/rss/search?q=台灣+ETF+換股+成分股&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "region": "tw",
    },
    # ── 美股（費半指數對台灣科技股影響大）──────────────────────
    {
        "name":   "費半/美股科技",
        "url":    "https://news.google.com/rss/search?q=Philadelphia+semiconductor+TSMC+stock&hl=en-US&gl=US&ceid=US:en",
        "region": "us",
    },
    # ── 可在此繼續新增其他 RSS 來源 ─────────────────────────────
    # Google News RSS 格式：
    # https://news.google.com/rss/search?q=<搜尋詞>&hl=zh-TW&gl=TW&ceid=TW:zh-Hant
    # {
    #     "name":   "自訂來源名稱",
    #     "url":    "https://news.google.com/rss/search?q=搜尋詞&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
    #     "region": "tw",
    # },
]

# 用來識別股票代號的正則（4-6位數字，前後有括號或特殊分隔）
STOCK_CODE_RE = re.compile(r'(?<!\d)(\d{4,6})(?!\d)')

# 需包含的關鍵字（利多相關）
POSITIVE_KEYWORDS = [
    # 台股中文
    "利多", "大漲", "創高", "突破", "法說", "拿單", "訂單", "超預期",
    "獲利", "股利", "配息", "成長", "看好", "買進", "轉機", "升評",
    "入列", "成分股", "調升", "主力", "外資買超",
    # 美股英文（費半/科技相關）
    "surge", "rally", "beat", "record high", "upgrade", "buy rating",
    "strong demand", "AI boom", "chip demand", "semiconductor",
]

NEGATIVE_KEYWORDS = [
    # 台股中文
    "利空", "大跌", "崩跌", "停牌", "下市", "虧損", "裁員", "減資",
    "警示", "全額交割",
    # 美股英文
    "plunge", "crash", "downgrade", "sell rating", "miss", "tariff",
    "trade war", "recession", "layoff", "ban",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _detect_sentiment(text_: str) -> str:
    pos = sum(1 for k in POSITIVE_KEYWORDS if k in text_)
    neg = sum(1 for k in NEGATIVE_KEYWORDS if k in text_)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def _extract_stocks(text_: str) -> list[str]:
    """從文字中提取可能的台股代號（4位數字）"""
    codes = STOCK_CODE_RE.findall(text_)
    # 只取 4 位數的股票代號（過濾掉年份、金額等）
    valid = [c for c in set(codes) if len(c) == 4 and c[0] in "012356789"]
    return sorted(valid)[:10]


def _already_saved(session, title: str, signal_date: date) -> bool:
    r = session.execute(text("""
        SELECT 1 FROM market_signals
        WHERE title = :title AND signal_date = :dt LIMIT 1
    """), {"title": title[:200], "dt": signal_date}).fetchone()
    return r is not None


# ── RSS 抓取 ──────────────────────────────────────────────────────
def fetch_rss_news(max_items: int = 30) -> int:
    """抓取 RSS 新聞並寫入 market_signals，回傳新增筆數"""
    saved = 0
    today = date.today()

    for source in RSS_SOURCES:
        logger.info(f"  爬取 RSS：{source['name']} [{source.get('region','tw').upper()}]")
        try:
            resp = requests.get(source["url"], headers=HEADERS, timeout=15)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as e:
            logger.warning(f"  RSS 抓取失敗 {source['name']}: {e}")
            continue

        items = root.findall(".//item")[:max_items]
        with get_session() as session:
            for item in items:
                title_el = item.find("title")
                desc_el  = item.find("description")
                link_el  = item.find("link")
                pub_el   = item.find("pubDate")

                if title_el is None:
                    continue

                title   = (title_el.text or "").strip()
                desc    = (desc_el.text  or "").strip() if desc_el is not None else ""
                url     = (link_el.text  or "").strip() if link_el is not None else None

                # 解析發布日期（相容 RFC 822 / ISO 8601 / 各種格式）
                pub_date = today
                try:
                    pub_str = (pub_el.text or "").strip() if pub_el is not None else ""
                    if pub_str:
                        for fmt in ("%a, %d %b %Y %H:%M:%S %z",
                                    "%a, %d %b %Y %H:%M:%S GMT",
                                    "%a, %d %b %Y"):
                            try:
                                pub_date = datetime.strptime(pub_str[:31], fmt).date()
                                break
                            except ValueError:
                                continue
                        else:
                            pub_date = datetime.fromisoformat(pub_str[:10]).date()
                except Exception:
                    pub_date = today

                if _already_saved(session, title, pub_date):
                    continue

                full_text = title + " " + desc
                sentiment = _detect_sentiment(full_text)
                stocks    = _extract_stocks(full_text)

                # 只存有利多/利空關鍵字 或 有股票代號的新聞，過濾雜訊
                if sentiment == "neutral" and not stocks:
                    continue

                session.execute(text("""
                    INSERT INTO market_signals
                        (signal_type, source, title, summary, url,
                         related_stocks, sentiment, signal_date)
                    VALUES
                        (:signal_type, :source, :title, :summary, :url,
                         :related_stocks, :sentiment, :signal_date)
                    ON CONFLICT DO NOTHING
                """), {
                    "signal_type":    "news",
                    "source":         source["name"],
                    "title":          title[:300],
                    "summary":        desc[:500] if desc else None,
                    "url":            url,
                    "related_stocks": stocks or None,
                    "sentiment":      sentiment,
                    "signal_date":    pub_date,
                })
                saved += 1

    logger.info(f"  RSS 新聞：新增 {saved} 筆")
    return saved


# ── MOPS 重大訊息 ─────────────────────────────────────────────────
def fetch_mops_announcements(max_items: int = 30) -> int:
    """
    抓取 MOPS（公開資訊觀測站）重大訊息
    使用 MOPS 公開 JSON API
    """
    saved = 0
    today = date.today()
    url = "https://mops.twse.com.tw/mops/web/ajax_t05sr01_1"

    params = {
        "encodeURIComponent": 1,
        "step":               1,
        "firstin":            1,
        "off":                1,
        "keyword4":           "",
        "code1":              "",
        "TYPEK2":             "",
        "checkbtn":           "",
        "queryName":          "co_id",
        "inpuType":           "co_id",
        "TYPEK":              "all",
        "isnew":              "true",
    }

    try:
        resp = requests.post(url, data=params, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            logger.warning(f"  MOPS API 回應 {resp.status_code}")
            return 0

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table tr")[1:max_items+1]

        with get_session() as session:
            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 5:
                    continue

                stock_id  = cells[0].get_text(strip=True)
                title     = cells[4].get_text(strip=True)
                pub_str   = cells[2].get_text(strip=True)

                if not title or not stock_id:
                    continue

                try:
                    # 民國年 → 西元年
                    parts = pub_str.split("/")
                    if len(parts) == 3:
                        roc_year = int(parts[0]) + 1911
                        pub_date = date(roc_year, int(parts[1]), int(parts[2]))
                    else:
                        pub_date = today
                except Exception:
                    pub_date = today

                if _already_saved(session, title, pub_date):
                    continue

                full_text = title
                sentiment = _detect_sentiment(full_text)

                session.execute(text("""
                    INSERT INTO market_signals
                        (signal_type, source, title, summary, url,
                         related_stocks, sentiment, signal_date)
                    VALUES
                        (:signal_type, :source, :title, :summary, :url,
                         :related_stocks, :sentiment, :signal_date)
                    ON CONFLICT DO NOTHING
                """), {
                    "signal_type":    "mops",
                    "source":         f"MOPS重大訊息",
                    "title":          f"【{stock_id}】{title[:250]}",
                    "summary":        None,
                    "url":            None,
                    "related_stocks": [stock_id] if stock_id.isdigit() else None,
                    "sentiment":      sentiment,
                    "signal_date":    pub_date,
                })
                saved += 1

    except Exception as e:
        logger.error(f"  MOPS 抓取失敗: {e}")

    logger.info(f"  MOPS 重大訊息：新增 {saved} 筆")
    return saved


# ── 主入口 ────────────────────────────────────────────────────────
def run_news_scraper():
    logger.info("=== 財經新聞爬取開始 ===")
    n1 = fetch_rss_news()
    n2 = fetch_mops_announcements()
    logger.info(f"=== 財經新聞爬取完成：共新增 {n1 + n2} 筆情報 ===")
    return n1 + n2
