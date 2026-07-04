"""
data_pipeline/scrapers/news_scraper.py

財經新聞爬蟲
  來源：Google News RSS（台股利多利空 / 半導體 / ETF / 費半美股）
  每篇文章用 Gemini 批次分析，生成精華重點摘要
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
from config.settings import APIConfig, tw_today


# ── RSS 來源設定 ─────────────────────────────────────────────────
# 要新增來源：直接在列表裡新增一個 dict，不需要修改其他地方
# Google News RSS 格式：https://news.google.com/rss/search?q=<關鍵字>&hl=zh-TW&gl=TW&ceid=TW:zh-Hant
RSS_SOURCES = [
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
    {
        "name":   "費半/美股科技",
        "url":    "https://news.google.com/rss/search?q=Philadelphia+semiconductor+TSMC+stock&hl=en-US&gl=US&ceid=US:en",
        "region": "us",
    },
    {
        "name":   "川普/政策動態",
        "url":    "https://news.google.com/rss/search?q=川普+關稅+半導體+台灣&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "region": "tw",
    },
    {
        "name":   "美股/總經",
        "url":    "https://news.google.com/rss/search?q=美股+Fed+利率+decision&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "region": "tw",
    },
    # 要加新來源：
    # {
    #     "name":   "自訂名稱",
    #     "url":    "https://news.google.com/rss/search?q=關鍵字&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
    #     "region": "tw",
    # },
]

STOCK_CODE_RE = re.compile(r'(?<!\d)(\d{4,6})(?!\d)')

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


# ── 工具函式 ──────────────────────────────────────────────────────
def _parse_pub_date(pub_str: str) -> date:
    today = tw_today()
    if not pub_str:
        return today
    for fmt in ("%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S GMT",
                "%a, %d %b %Y"):
        try:
            return datetime.strptime(pub_str[:31].strip(), fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(pub_str[:10]).date()
    except Exception:
        return today


def _already_saved(session, title: str, signal_date: date) -> bool:
    """已存在且有實質 AI 摘要（>20 字）才視為已完成；summary 空的讓它重新分析。"""
    r = session.execute(text("""
        SELECT summary FROM market_signals
        WHERE title = :title AND signal_date = :dt LIMIT 1
    """), {"title": title[:200], "dt": signal_date}).fetchone()
    if r is None:
        return False
    return bool(r[0] and len(r[0].strip()) > 20)


# ── Gemini 回應行解析（獨立函式，供單元測試）──────────────────────
def _parse_analysis_line(line: str, default_summary: str) -> dict:
    """解析 '[N] 重點：xx | 情緒：正面 | 股票：2330,2454' 格式的一行。"""
    summary, stocks, sentiment = default_summary, [], "neutral"
    if line:
        line = re.sub(r'^\[\d+\]\s*', '', line).strip()
        for part in line.split("|"):
            part = part.strip()
            if "重點：" in part:
                v = part.split("重點：", 1)[-1].strip()
                if v:
                    summary = v
            elif "情緒：" in part:
                v = part.split("情緒：", 1)[-1].strip()
                sentiment = ("positive" if "正面" in v
                             else "negative" if "負面" in v
                             else "neutral")
            elif "股票：" in part:
                v = part.split("股票：", 1)[-1].strip()
                if v != "無":
                    stocks = [c for c in re.findall(r'\d{4}', v)][:10]
    return {"summary": summary, "stocks": stocks, "sentiment": sentiment}


# ── Gemini 批次分析新聞 ───────────────────────────────────────────
def _batch_analyze(articles: list[dict]) -> list[dict]:
    """
    一次 Gemini 呼叫分析多則新聞。
    articles: [{title, desc}]
    return: [{summary, stocks, sentiment}]
    """
    fallback = [
        {"summary": (a.get("desc") or a["title"])[:300],
         "stocks": [], "sentiment": "neutral"}
        for a in articles
    ]

    if not APIConfig.GEMINI_API_KEY or not articles:
        return fallback

    items_text = "\n".join(
        f"[{i+1}] 標題：{a['title'][:120]}"
        + (f"\n     原文摘要：{a['desc'][:200]}" if a.get("desc") else "")
        for i, a in enumerate(articles)
    )

    prompt = f"""你是台灣股票分析師。以下是 {len(articles)} 則財經新聞，請為每則生成精簡分析。

{items_text}

嚴格按照以下格式回答（每行一則，共 {len(articles)} 行，不要加任何其他文字）：
[1] 重點：<2句繁體中文精華重點> | 情緒：<正面/負面/中立> | 股票：<台股4位數代號逗號分隔，無則填無>
[2] 重點：... | 情緒：... | 股票：...
..."""

    try:
        import litellm
        response = litellm.completion(
            model=APIConfig.GEMINI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            api_key=APIConfig.GEMINI_API_KEY,
            max_tokens=min(len(articles) * 100 + 200, 4000),
            reasoning_effort="disable",  # gemini-2.5 預設會思考，關閉以免吃光 max_tokens
        )
        text_ = response.choices[0].message.content or ""

        results = []
        for i, a in enumerate(articles):
            line = next(
                (l for l in text_.splitlines() if l.strip().startswith(f"[{i+1}]")),
                ""
            )
            default_summary = (a.get("desc") or a["title"])[:300]
            results.append(_parse_analysis_line(line, default_summary))

        return results

    except Exception as e:
        logger.warning(f"  Gemini 批次分析失敗: {e}")
        return fallback


# ── RSS 抓取 ──────────────────────────────────────────────────────
def fetch_rss_news(max_items: int = 30) -> int:
    """抓取 RSS 新聞 → Gemini 批次分析 → 寫入 market_signals。回傳新增筆數"""
    today = tw_today()

    # Step 1：收集所有尚未存入 DB 的新文章
    pending = []
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

                raw_title = (title_el.text or "").strip()
                # Google News title 格式常是 "新聞標題 - 來源媒體"，去掉媒體名稱
                title = re.sub(r'\s*-\s*[^-]+$', '', raw_title).strip() or raw_title
                # description 可能含 CDATA HTML（Google News RSS 常見），用 BeautifulSoup 確保乾淨
                raw_desc = (desc_el.text or "") if desc_el is not None else ""
                try:
                    from bs4 import BeautifulSoup as _BS
                    desc = _BS(raw_desc, "html.parser").get_text(separator=" ").strip()[:400]
                except Exception:
                    desc = re.sub(r'<[^>]+>', '', raw_desc).strip()[:400]
                url   = (link_el.text or "").strip() if link_el is not None else None
                pub_date = _parse_pub_date((pub_el.text or "") if pub_el is not None else "")

                if _already_saved(session, title, pub_date):
                    continue

                pending.append({
                    "title":    title[:300],
                    "desc":     desc,
                    "url":      url,
                    "pub_date": pub_date,
                    "source":   source["name"],
                })

    if not pending:
        logger.info("  RSS 新聞：無新文章")
        return 0

    logger.info(f"  RSS 新聞：共 {len(pending)} 篇待分析")

    # Step 2：批次 Gemini 分析（每批 20 篇）
    BATCH = 20
    analyses = []
    for i in range(0, len(pending), BATCH):
        batch = pending[i:i + BATCH]
        logger.info(f"  Gemini 分析第 {i//BATCH + 1} 批（{len(batch)} 篇）...")
        analyses.extend(_batch_analyze(batch))

    # Step 3：全部存入 DB（有舊記錄但 summary 空 → UPDATE；全新 → INSERT）
    saved = 0
    with get_session() as session:
        for article, analysis in zip(pending, analyses):
            # 過濾：分析完全失敗（中立 + 無股票 + 摘要 = desc）→ 跳過
            if (analysis["sentiment"] == "neutral"
                    and not analysis["stocks"]
                    and (not analysis["summary"] or analysis["summary"] == article["desc"])):
                continue

            summary_val = analysis["summary"][:500] if analysis["summary"] else None

            # 先嘗試 UPDATE 現有無摘要的記錄
            updated = session.execute(text("""
                UPDATE market_signals
                SET summary = :summary, related_stocks = :related_stocks,
                    sentiment = :sentiment, url = COALESCE(url, :url)
                WHERE title = :title AND signal_date = :dt
                  AND (summary IS NULL OR length(summary) <= 20)
            """), {
                "summary":        summary_val,
                "related_stocks": analysis["stocks"] or None,
                "sentiment":      analysis["sentiment"],
                "url":            article["url"],
                "title":          article["title"],
                "dt":             article["pub_date"],
            }).rowcount

            if updated == 0:
                # 全新文章 → INSERT
                session.execute(text("""
                    INSERT INTO market_signals
                        (signal_type, source, title, summary, url,
                         related_stocks, sentiment, signal_date)
                    VALUES
                        (:signal_type, :source, :title, :summary, :url,
                         :related_stocks, :sentiment, :signal_date)
                """), {
                    "signal_type":    "news",
                    "source":         article["source"],
                    "title":          article["title"],
                    "summary":        summary_val,
                    "url":            article["url"],
                    "related_stocks": analysis["stocks"] or None,
                    "sentiment":      analysis["sentiment"],
                    "signal_date":    article["pub_date"],
                })
            saved += 1

    logger.info(f"  RSS 新聞：新增/更新 {saved} 筆（含 AI 摘要）")
    return saved


# ── MOPS 重大訊息 ─────────────────────────────────────────────────
def fetch_mops_announcements(max_items: int = 30) -> int:
    saved = 0
    today = tw_today()
    url = "https://mops.twse.com.tw/mops/web/ajax_t05sr01_1"
    params = {
        "encodeURIComponent": 1, "step": 1, "firstin": 1, "off": 1,
        "keyword4": "", "code1": "", "TYPEK2": "", "checkbtn": "",
        "queryName": "co_id", "inpuType": "co_id",
        "TYPEK": "all", "isnew": "true",
    }
    try:
        resp = requests.post(url, data=params, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            logger.warning(f"  MOPS API 回應 {resp.status_code}")
            return 0
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table tr")[1:max_items + 1]
        with get_session() as session:
            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 5:
                    continue
                stock_id = cells[0].get_text(strip=True)
                title    = cells[4].get_text(strip=True)
                pub_str  = cells[2].get_text(strip=True)
                if not title or not stock_id:
                    continue
                try:
                    parts = pub_str.split("/")
                    pub_date = date(int(parts[0]) + 1911, int(parts[1]), int(parts[2])) if len(parts) == 3 else today
                except Exception:
                    pub_date = today
                if _already_saved(session, title, pub_date):
                    continue
                session.execute(text("""
                    INSERT INTO market_signals
                        (signal_type, source, title, related_stocks, signal_date)
                    VALUES
                        ('mops', 'MOPS重大訊息', :title, :stocks, :dt)
                    ON CONFLICT DO NOTHING
                """), {
                    "title":  f"【{stock_id}】{title[:250]}",
                    "stocks": [stock_id] if stock_id.isdigit() else None,
                    "dt":     pub_date,
                })
                saved += 1
    except Exception as e:
        logger.error(f"  MOPS 抓取失敗: {e}")

    logger.info(f"  MOPS 重大訊息：新增 {saved} 筆")
    return saved


# ── 鉅亨網深度新聞（有完整內文 → 5~10 行條列重點）──────────────────
# Google News RSS 只有標題（連結是 JS 跳轉抓不到全文），摘要品質受限；
# 鉅亨公開 API 的 content 欄位有完整文章，才能生成真正的重點整理。
_CNYES_API = "https://api.cnyes.com/media/api/v1/newslist/category/{cat}?limit={limit}&page=1"
_CNYES_CATS = ["tw_stock"]          # 要加分類：wd_stock(國際)、future(期貨)...
_CNYES_HEADERS = {**HEADERS, "Origin": "https://news.cnyes.com",
                  "Referer": "https://news.cnyes.com/"}


def _cnyes_deep_analyze(articles: list[dict]) -> list[dict]:
    """
    一次 Gemini 呼叫分析一批（含全文），輸出 JSON：
    {"items":[{"idx":1,"summary":["句1",...],"sentiment":"正面","stocks":["2330"]}]}
    """
    fallback = [{"summary": [a["title"]], "stocks": [], "sentiment": "neutral"}
                for a in articles]
    if not APIConfig.GEMINI_API_KEY or not articles:
        return fallback

    body = "\n\n".join(
        f"【第 {i+1} 篇】{a['title']}\n{a['content'][:1500]}"
        for i, a in enumerate(articles)
    )
    prompt = f"""你是台灣股票分析師。以下 {len(articles)} 篇財經新聞（含全文），為每篇整理重點。

{body}

輸出 JSON（不要其他文字）：
{{"items":[{{"idx":1,"summary":["重點句1","重點句2","重點句3"],"sentiment":"正面|負面|中立","stocks":["4位數代號"]}}]}}

summary 要求：3~5 句條列，每句獨立可讀，涵蓋「發生什麼事、影響哪些公司/族群、關鍵數字、後續觀察點」。
stocks 只列文中明確提及的台股代號，無則空陣列。"""

    try:
        import litellm, json as _json
        resp = litellm.completion(
            model=APIConfig.GEMINI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            api_key=APIConfig.GEMINI_API_KEY,
            max_tokens=min(len(articles) * 350 + 300, 6000),
            response_format={"type": "json_object"},
            reasoning_effort="disable",
        )
        parsed = _json.loads(resp.choices[0].message.content or "{}")
        items = {it.get("idx"): it for it in parsed.get("items", [])}
        out = []
        for i, a in enumerate(articles):
            it = items.get(i + 1, {})
            sents = it.get("summary") or [a["title"]]
            sent_map = {"正面": "positive", "負面": "negative"}
            out.append({
                "summary":   [str(s).strip() for s in sents if str(s).strip()][:6],
                "stocks":    [c for c in it.get("stocks", []) if re.fullmatch(r"\d{4}", str(c))][:10],
                "sentiment": sent_map.get(str(it.get("sentiment", "")).strip(), "neutral"),
            })
        return out
    except Exception as e:
        logger.warning(f"  鉅亨深度分析失敗: {e}")
        return fallback


def fetch_cnyes_news(limit: int = 15) -> int:
    """鉅亨 API 抓含全文的新聞 → Gemini 條列重點 → market_signals。"""
    from datetime import datetime as _dt
    saved = 0
    pending = []

    for cat in _CNYES_CATS:
        try:
            r = requests.get(_CNYES_API.format(cat=cat, limit=limit),
                             headers=_CNYES_HEADERS, timeout=20)
            r.raise_for_status()
            data = r.json().get("items", {}).get("data", [])
        except Exception as e:
            logger.warning(f"  鉅亨 API 失敗 {cat}: {e}")
            continue

        with get_session() as session:
            for it in data:
                title = (it.get("title") or "").strip()
                nid   = it.get("newsId")
                if not title or not nid:
                    continue
                ts = it.get("publishAt")
                try:
                    pub_date = _dt.fromtimestamp(int(ts)).date() if ts else tw_today()
                except Exception:
                    pub_date = tw_today()
                if _already_saved(session, title, pub_date):
                    continue
                content = re.sub(r"<[^>]+>", " ", it.get("content") or "")
                content = re.sub(r"\s+", " ", content).strip()
                if len(content) < 80:      # 內文太短沒有分析價值
                    continue
                pending.append({
                    "title": title[:300], "content": content,
                    "url": f"https://news.cnyes.com/news/id/{nid}",
                    "pub_date": pub_date,
                })

    if not pending:
        logger.info("  鉅亨新聞：無新文章")
        return 0

    logger.info(f"  鉅亨新聞：{len(pending)} 篇待深度分析")
    BATCH = 5   # 含全文，批次小一點確保輸出完整
    analyses = []
    for i in range(0, len(pending), BATCH):
        analyses.extend(_cnyes_deep_analyze(pending[i:i + BATCH]))

    with get_session() as session:
        for art, ana in zip(pending, analyses):
            summary_text = "\n".join(f"• {s}" for s in ana["summary"])
            session.execute(text("""
                INSERT INTO market_signals
                    (signal_type, source, title, summary, url,
                     related_stocks, sentiment, signal_date)
                VALUES ('news', '鉅亨網', :t, :su, :u, :st, :se, :dt)
                ON CONFLICT DO NOTHING
            """), {"t": art["title"], "su": summary_text[:1500], "u": art["url"],
                   "st": ana["stocks"] or None, "se": ana["sentiment"],
                   "dt": art["pub_date"]})
            saved += 1

    logger.info(f"  鉅亨新聞：新增 {saved} 筆（條列重點）")
    return saved


# ── 主入口 ────────────────────────────────────────────────────────
def run_news_scraper():
    logger.info("=== 財經新聞爬取開始 ===")
    n0 = fetch_cnyes_news()          # 深度摘要（有全文）
    n1 = fetch_rss_news()            # 廣度掃描（Google News，僅標題）
    n2 = fetch_mops_announcements()
    logger.info(f"=== 財經新聞爬取完成：共新增 {n0 + n1 + n2} 筆 ===")
    return n0 + n1 + n2
