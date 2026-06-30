"""
data_pipeline/scrapers/youtube_scraper.py

YouTube 財經頻道摘要
  - 透過 YouTube RSS 取得最新影片（無需 API key）
  - youtube-transcript-api 抓字幕（繁中 / 自動字幕）
  - Gemini 分析：摘要 + 提及股票 + 情緒
  - 結果寫入 market_signals 表
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
from config.settings import APIConfig


# ── 追蹤的 YouTube 頻道 ──────────────────────────────────────────
YOUTUBE_CHANNELS = [
    {
        "name":       "錢線百分百",
        "channel_id": "UCdtpDTFXDvpblTt0TRMfuKQ",
        "max_videos": 3,
    },
    {
        "name":       "股市大富翁",
        "channel_id": "UCqFPD5HJFXkDDPJnfyiPWyA",
        "max_videos": 2,
    },
]

RSS_BASE = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
WATCH_BASE = "https://www.youtube.com/watch?v={video_id}"

STOCK_CODE_RE = re.compile(r'(?<!\d)(\d{4,6})(?!\d)')

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


# ── 從 RSS 抓最新影片清單 ──────────────────────────────────────
def _fetch_channel_videos(channel_id: str, max_videos: int = 3) -> list[dict]:
    """回傳 [{video_id, title, published}]"""
    url = RSS_BASE.format(channel_id=channel_id)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        logger.warning(f"  YouTube RSS 失敗 {channel_id}: {e}")
        return []

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt":   "http://www.youtube.com/xml/schemas/2015",
    }
    videos = []
    for entry in root.findall("atom:entry", ns)[:max_videos]:
        vid_id = entry.findtext("yt:videoId", namespaces=ns)
        title  = entry.findtext("atom:title", namespaces=ns) or ""
        pub    = entry.findtext("atom:published", namespaces=ns) or ""
        if vid_id:
            try:
                pub_date = datetime.fromisoformat(pub[:10]).date()
            except Exception:
                pub_date = date.today()
            videos.append({"video_id": vid_id, "title": title, "published": pub_date})

    return videos


# ── 抓字幕 ──────────────────────────────────────────────────────
def _get_transcript(video_id: str, max_chars: int = 4000) -> str | None:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound
        # 優先繁中，其次簡中，最後自動生成
        langs = ["zh-TW", "zh-Hant", "zh", "zh-Hans", "zh-CN"]
        try:
            snippets = YouTubeTranscriptApi.get_transcript(video_id, languages=langs)
        except NoTranscriptFound:
            snippets = YouTubeTranscriptApi.get_transcript(video_id)

        text_ = " ".join(s["text"] for s in snippets)
        return text_[:max_chars]
    except Exception as e:
        logger.debug(f"  字幕取得失敗 {video_id}: {e}")
        return None


# ── Gemini 分析 ──────────────────────────────────────────────────
def _analyze_with_gemini(channel_name: str, title: str, transcript: str) -> dict:
    """
    回傳 {summary, stocks, sentiment}
    即使 Gemini 失敗也回傳預設值
    """
    default = {
        "summary":   title,
        "stocks":    [],
        "sentiment": "neutral",
    }

    if not APIConfig.GEMINI_API_KEY:
        return default

    prompt = f"""你是台灣股票市場分析助手。以下是 YouTube 財經節目「{channel_name}」的影片字幕片段：

標題：{title}

字幕（節錄）：
{transcript}

請用繁體中文回答以下問題（不要有多餘格式，直接回答）：
1. 簡短摘要（2-3句話，含主要觀點或市場看法）
2. 提及的台股代號（只列出4位數字代號，用逗號分隔，沒有則填無）
3. 整體情緒（只回答：正面、負面、中立 其中一個）

格式：
摘要：<摘要>
股票：<代號列表>
情緒：<情緒>"""

    try:
        import litellm
        response = litellm.completion(
            model="gemini/gemini-1.5-flash",
            messages=[{"role": "user", "content": prompt}],
            api_key=APIConfig.GEMINI_API_KEY,
            max_tokens=400,
        )
        text_ = response.choices[0].message.content or ""

        summary, stocks, sentiment = default["summary"], [], default["sentiment"]
        for line in text_.splitlines():
            if line.startswith("摘要："):
                summary = line[3:].strip()
            elif line.startswith("股票："):
                raw = line[3:].strip()
                codes = STOCK_CODE_RE.findall(raw)
                stocks = [c for c in set(codes) if len(c) == 4][:10]
            elif line.startswith("情緒："):
                val = line[3:].strip()
                if "正面" in val:
                    sentiment = "positive"
                elif "負面" in val:
                    sentiment = "negative"
                else:
                    sentiment = "neutral"

        return {"summary": summary, "stocks": stocks, "sentiment": sentiment}

    except Exception as e:
        logger.warning(f"  Gemini 分析失敗: {e}")
        return default


# ── 寫入 market_signals ──────────────────────────────────────────
def _already_saved(session, video_id: str) -> bool:
    r = session.execute(text("""
        SELECT 1 FROM market_signals
        WHERE url LIKE :pattern AND signal_type = 'youtube' LIMIT 1
    """), {"pattern": f"%{video_id}%"}).fetchone()
    return r is not None


# ── 主入口 ──────────────────────────────────────────────────────
def run_youtube_scraper():
    logger.info("=== YouTube 財經頻道分析開始 ===")
    saved = 0

    for ch in YOUTUBE_CHANNELS:
        channel_name = ch["name"]
        logger.info(f"  頻道：{channel_name}")
        videos = _fetch_channel_videos(ch["channel_id"], max_videos=ch["max_videos"])

        if not videos:
            logger.warning(f"    {channel_name}：無法取得影片列表")
            continue

        for v in videos:
            vid_id   = v["video_id"]
            title    = v["title"]
            pub_date = v["published"]
            watch_url = WATCH_BASE.format(video_id=vid_id)

            with get_session() as session:
                if _already_saved(session, vid_id):
                    logger.debug(f"    已存在，跳過：{title[:40]}")
                    continue

            # 只處理最近 3 天的影片（避免處理太舊的）
            if (date.today() - pub_date).days > 3:
                logger.debug(f"    超過 3 天，跳過：{title[:40]}")
                continue

            logger.info(f"    處理影片：{title[:50]}")
            transcript = _get_transcript(vid_id)

            if transcript:
                analysis = _analyze_with_gemini(channel_name, title, transcript)
            else:
                # 沒有字幕：只存標題，不做 AI 分析
                logger.info(f"    無字幕，僅存標題")
                analysis = {
                    "summary":   title,
                    "stocks":    [],
                    "sentiment": "neutral",
                }

            with get_session() as session:
                session.execute(text("""
                    INSERT INTO market_signals
                        (signal_type, source, title, summary, url,
                         related_stocks, sentiment, signal_date)
                    VALUES
                        (:signal_type, :source, :title, :summary, :url,
                         :related_stocks, :sentiment, :signal_date)
                    ON CONFLICT DO NOTHING
                """), {
                    "signal_type":    "youtube",
                    "source":         channel_name,
                    "title":          f"【{channel_name}】{title[:250]}",
                    "summary":        analysis["summary"][:500] if analysis["summary"] else None,
                    "url":            watch_url,
                    "related_stocks": analysis["stocks"] or None,
                    "sentiment":      analysis["sentiment"],
                    "signal_date":    pub_date,
                })
                saved += 1

    logger.info(f"=== YouTube 分析完成：新增 {saved} 筆 ===")
    return saved
