"""
data_pipeline/scrapers/youtube_scraper.py

YouTube 財經頻道摘要
  - 透過 YouTube RSS 取得頻道所有最新影片（無需 API key）
  - 每次執行只處理「上次執行後」新發布的影片，同一天 3 集也全部處理
  - youtube-transcript-api 抓字幕（繁中 / 自動字幕）
  - Gemini 分析：摘要 + 提及股票 + 情緒
  - 結果寫入 market_signals 表

要新增追蹤頻道：在 YOUTUBE_CHANNELS 列表加一個 dict 即可。
channel_id 取得方式：開頻道頁面 → 右鍵「檢視原始碼」→ 搜尋 "channelId"
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
import re
import requests
from loguru import logger
from sqlalchemy import text

from database.connection import get_session
from config.settings import APIConfig, tw_today


# ── 追蹤的 YouTube 頻道 ──────────────────────────────────────────
# 要新增頻道：直接在這裡加 dict，不需要修改其他任何地方
YOUTUBE_CHANNELS = [
    {
        "name":       "錢線百分百",
        "channel_id": "UC_ObC9O0ZQ2FhW6u9_iFlZA",
    },
    # 要加新頻道，複製下面這段並填入 channel_id：
    # {
    #     "name":       "頻道顯示名稱",
    #     "channel_id": "UCxxxxxxxxxxxxxxxxxx",
    # },
]

# 只處理最近幾天內發布的影片（避免第一次執行時把整個頻道歷史都爬一遍）
MAX_LOOKBACK_DAYS = 2

# RSS 每個頻道最多抓幾部影片（設高一點以確保當天所有影片都能被拿到）
RSS_MAX_ENTRIES = 15

RSS_BASE   = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
WATCH_BASE = "https://www.youtube.com/watch?v={video_id}"

STOCK_CODE_RE = re.compile(r'(?<!\d)(\d{4,6})(?!\d)')

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


# ── 從 RSS 抓最新影片清單 ────────────────────────────────────────
def _fetch_channel_videos(channel_id: str) -> list[dict]:
    """回傳 [{video_id, title, published}]，最多 RSS_MAX_ENTRIES 筆。
    YouTube RSS 偶發 500，自動 retry 2 次。"""
    import time
    url = RSS_BASE.format(channel_id=channel_id)
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 500 and attempt < 2:
                logger.debug(f"  YouTube RSS 500，{attempt+1}/3 次 retry...")
                time.sleep(3)
                continue
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            break
        except Exception as e:
            if attempt == 2:
                logger.warning(f"  YouTube RSS 失敗 {channel_id}: {e}")
                return []
            time.sleep(3)
    else:
        return []

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt":   "http://www.youtube.com/xml/schemas/2015",
    }
    videos = []
    for entry in root.findall("atom:entry", ns)[:RSS_MAX_ENTRIES]:
        vid_id = entry.findtext("yt:videoId", namespaces=ns)
        title  = entry.findtext("atom:title",  namespaces=ns) or ""
        pub    = entry.findtext("atom:published", namespaces=ns) or ""
        if vid_id:
            try:
                pub_date = datetime.fromisoformat(pub[:10]).date()
            except Exception:
                pub_date = tw_today()
            videos.append({"video_id": vid_id, "title": title, "published": pub_date})

    return videos


# ── 判斷影片是否已儲存 ────────────────────────────────────────────
def _already_saved(session, video_id: str) -> bool:
    """URL 存在即視為已處理（避免重複；無摘要的情況靠刪除舊資料重跑）。"""
    r = session.execute(text("""
        SELECT 1 FROM market_signals
        WHERE url LIKE :pattern AND signal_type = 'youtube' LIMIT 1
    """), {"pattern": f"%{video_id}%"}).fetchone()
    return r is not None


# ── 抓字幕 ───────────────────────────────────────────────────────
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

請用繁體中文整理這集節目重點（讓人 5~10 行內看完整集精華）：

摘要：
- <重點1：大盤/市場觀點>
- <重點2：看好或看壞的族群與理由>
- <重點3：提及的個股與觀點>
- <重點4：風險提醒或操作建議>
（4~6 點，每點一句話，只寫字幕中真的有講的內容）
股票：<提及的台股4位數代號，逗號分隔，無則填無>
情緒：<正面/負面/中立>"""

    try:
        import litellm
        response = litellm.completion(
            model=APIConfig.GEMINI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            api_key=APIConfig.GEMINI_API_KEY,
            max_tokens=500,
            reasoning_effort="disable",  # gemini-2.5 預設會思考，關閉以免吃光 max_tokens
        )
        text_ = response.choices[0].message.content or ""

        summary, stocks, sentiment = default["summary"], [], default["sentiment"]
        bullets, in_summary = [], False
        for line in text_.splitlines():
            stripped = line.strip()
            if stripped.startswith("摘要："):
                in_summary = True
                rest = stripped[3:].strip()
                if rest:                      # 摘要與第一點同行的情況
                    bullets.append(rest.lstrip("-• "))
            elif stripped.startswith("股票："):
                in_summary = False
                raw = stripped[3:].strip()
                codes = STOCK_CODE_RE.findall(raw)
                stocks = [c for c in dict.fromkeys(codes) if len(c) == 4][:10]
            elif stripped.startswith("情緒："):
                in_summary = False
                val = stripped[3:].strip()
                sentiment = ("positive" if "正面" in val
                             else "negative" if "負面" in val else "neutral")
            elif in_summary and stripped.startswith(("-", "•", "・")):
                bullets.append(stripped.lstrip("-•・ ").strip())

        if bullets:
            summary = "\n".join(f"• {b}" for b in bullets if b)

        return {"summary": summary, "stocks": stocks, "sentiment": sentiment}

    except Exception as e:
        logger.warning(f"  Gemini 分析失敗: {e}")
        return default


def _analyze_title_only(channel_name: str, title: str) -> dict:
    """
    無字幕時的降級分析（雲端 IP 常被 YouTube 擋字幕）：
    只根據標題請 Gemini 推測主題與提及個股，明確標註為推測。
    """
    default = {"summary": None, "stocks": [], "sentiment": "neutral"}
    if not APIConfig.GEMINI_API_KEY:
        return default
    try:
        import litellm
        resp = litellm.completion(
            model=APIConfig.GEMINI_MODEL,
            messages=[{"role": "user", "content":
                f"台灣財經節目「{channel_name}」影片標題：{title}\n"
                "僅根據標題，用 1-2 句繁體中文推測本集主題與可能討論的個股/族群"
                "（有 4 位數代號就寫出來）。直接輸出推測內容，不要前綴。"}],
            api_key=APIConfig.GEMINI_API_KEY,
            max_tokens=200,
            reasoning_effort="disable",
        )
        guess = (resp.choices[0].message.content or "").strip()
        if not guess:
            return default
        codes = STOCK_CODE_RE.findall(guess)
        return {
            "summary": f"（無字幕，僅依標題推測）{guess}",
            "stocks": [c for c in dict.fromkeys(codes) if len(c) == 4][:10],
            "sentiment": "neutral",
        }
    except Exception as e:
        logger.debug(f"  標題推測失敗: {e}")
        return default


# ── 主入口 ───────────────────────────────────────────────────────
def run_youtube_scraper(lookback_days: int = MAX_LOOKBACK_DAYS):
    """
    掃描所有追蹤頻道，處理 lookback_days 天內發布的所有新影片。
    同一天發布 3 集的情況也能全部處理（RSS_MAX_ENTRIES=15）。
    已存入 DB 的影片（以 video_id 去重）自動跳過。
    """
    logger.info(f"=== YouTube 財經頻道分析開始（追蹤 {len(YOUTUBE_CHANNELS)} 個頻道，"
                f"看回 {lookback_days} 天）===")
    saved = 0
    cutoff = tw_today() - timedelta(days=lookback_days)

    for ch in YOUTUBE_CHANNELS:
        channel_name = ch["name"]
        logger.info(f"  頻道：{channel_name}")
        videos = _fetch_channel_videos(ch["channel_id"])

        if not videos:
            logger.warning(f"    {channel_name}：無法取得影片列表")
            continue

        # 過濾出 cutoff 之後的影片，全部處理（不限集數）
        new_videos = [v for v in videos if v["published"] >= cutoff]
        logger.info(f"    共 {len(videos)} 部影片，近 {lookback_days} 天新影片：{len(new_videos)} 部")

        if not new_videos:
            logger.info(f"    {channel_name}：近 {lookback_days} 天無新影片")
            continue

        for v in new_videos:
            vid_id    = v["video_id"]
            title     = v["title"]
            pub_date  = v["published"]
            watch_url = WATCH_BASE.format(video_id=vid_id)

            with get_session() as session:
                if _already_saved(session, vid_id):
                    logger.debug(f"    已存在，跳過：{title[:40]}")
                    continue

            logger.info(f"    [{pub_date}] 處理：{title[:50]}")
            transcript = _get_transcript(vid_id)

            if transcript:
                analysis = _analyze_with_gemini(channel_name, title, transcript)
                # Gemini 失敗時 summary = title（default），改存 None 避免誤判
                if analysis["summary"] == title:
                    analysis["summary"] = None
            else:
                logger.info(f"    無字幕 → 改用標題推測（降級模式）")
                analysis = _analyze_title_only(channel_name, title)

            with get_session() as session:
                session.execute(text("""
                        INSERT INTO market_signals
                            (signal_type, source, title, summary, url,
                             related_stocks, sentiment, signal_date)
                        VALUES
                            (:signal_type, :source, :title, :summary, :url,
                             :related_stocks, :sentiment, :signal_date)
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
