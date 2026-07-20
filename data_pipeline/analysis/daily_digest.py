"""
data_pipeline/analysis/daily_digest.py

每日市場情報彙整
  - 查詢當日所有 market_signals（新聞 / YouTube / ETF 換股）
  - 一次 Gemini 呼叫生成結構化每日摘要
  - 結果存回 market_signals（signal_type='digest'）
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from datetime import date
from loguru import logger
from sqlalchemy import text

from database.connection import get_session
from config.settings import APIConfig, tw_today


def get_latest_digest(days: int = 5) -> tuple[date, str] | None:
    """
    最近 days 天內最新一份彙整（不管本次呼叫是否剛生成），
    供 Telegram 每日報告與 /digest 指令共用。
    """
    with get_session() as session:
        row = session.execute(text("""
            SELECT signal_date, summary FROM market_signals
            WHERE signal_type = 'digest'
              AND signal_date >= CURRENT_DATE - :days * INTERVAL '1 day'
            ORDER BY signal_date DESC, id DESC LIMIT 1
        """), {"days": days}).fetchone()
    return (row[0], row[1]) if row else None


def generate_daily_digest(target_date: date = None) -> str | None:
    """
    彙整當日所有情報，用 Gemini 生成精簡每日總結。
    回傳摘要文字（供 Telegram / app 顯示），若無內容則回傳 None。
    """
    if target_date is None:
        target_date = tw_today()

    logger.info(f"=== 生成每日彙整：{target_date} ===")

    # 已有今日 digest → 跳過
    with get_session() as session:
        exists = session.execute(text("""
            SELECT 1 FROM market_signals
            WHERE signal_type = 'digest' AND signal_date = :dt LIMIT 1
        """), {"dt": target_date}).fetchone()
        if exists:
            logger.info("  今日彙整已存在，跳過")
            return None

        # 抓今日所有情報（不含 digest 本身與 smart_money——後者一天可達 60 筆，
        # 會塞爆 prompt 稀釋新聞/YT 重點；聰明資金已有專頁呈現）
        rows = session.execute(text("""
            SELECT signal_type, source, title, summary
            FROM market_signals
            WHERE signal_date = :dt
              AND signal_type NOT IN ('digest', 'smart_money')
            ORDER BY signal_type, id
            LIMIT 120
        """), {"dt": target_date}).fetchall()

    if not rows:
        logger.info("  今日無市場情報，跳過彙整")
        return None

    # 分類整理
    news_lines = []
    yt_lines   = []
    etf_lines  = []

    for signal_type, source, title, summary in rows:
        # 去掉【來源】前綴，只保留核心標題
        clean_title = title
        for prefix in ["【台股新聞】", "【台股半導體】", "【ETF新聞】", "【費半/美股科技】"]:
            clean_title = clean_title.replace(prefix, "")

        if signal_type in ("news", "mops"):
            line = f"- {clean_title.strip()}"
            if summary and summary != clean_title:
                line += f"：{summary[:150]}"
            news_lines.append(line)
        elif signal_type == "youtube":
            line = f"- {clean_title.strip()}"
            if summary and summary != clean_title:
                line += f"\n  摘要：{summary[:300]}"
            yt_lines.append(line)
        elif signal_type == "etf_change":
            etf_lines.append(f"- {clean_title.strip()}")

    sections = []
    if news_lines:
        sections.append("【財經新聞（共 {} 則）】\n".format(len(news_lines))
                        + "\n".join(news_lines[:40]))
    if yt_lines:
        sections.append("【YouTube 節目摘要】\n" + "\n".join(yt_lines))
    if etf_lines:
        sections.append("【ETF 換股動態】\n" + "\n".join(etf_lines[:20]))

    if not sections:
        return None

    content = "\n\n".join(sections)

    if not APIConfig.GEMINI_API_KEY:
        logger.warning("  未設定 GEMINI_API_KEY，跳過彙整")
        return None

    prompt = f"""你是台灣股票市場分析師。以下是 {target_date} 的台股市場情報：

{content}

請用繁體中文整理今日重點，格式如下（每項簡潔扼要，整體不超過 400 字）：

【被看好的族群】
（列出 2-4 個族群，每個說明 1 句看好理由）

【需注意的風險或利空】
（列出主要風險，1-3 點）

【新聞提及個股】
（如有明確提及，列：代號 名稱 — 理由；若無則寫「今日無特別提及」。
   這裡只是「今天新聞/YouTube/ETF換股動態有提到誰」的整理，不是選股推薦，
   不要暗示這些是值得買進或關注的標的——沒有經過任何篩選或評分。）

【今日市場氛圍】
（一句話：多頭/偏多/中性/偏空，及主要驅動因素）

直接以【被看好的族群】開頭，不要任何開場白或結語。"""

    try:
        import litellm
        response = litellm.completion(
            model=APIConfig.GEMINI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            api_key=APIConfig.GEMINI_API_KEY,
            max_tokens=800,
            reasoning_effort="disable",  # gemini-2.5 預設會思考，關閉以免吃光 max_tokens
        )
        digest_text = (response.choices[0].message.content or "").strip()

        if not digest_text:
            logger.warning("  Gemini 回應為空")
            return None

        # 去除 AI 開場白（如「好的，根據您提供的…」）——第一個【之前的文字都刪掉
        import re as _re
        cleaned = _re.sub(r"^[^【]*?(?=【)", "", digest_text, count=1).strip()
        if cleaned:
            digest_text = cleaned

        with get_session() as session:
            session.execute(text("""
                INSERT INTO market_signals
                    (signal_type, source, title, summary, signal_date)
                VALUES
                    ('digest', '每日彙整', :title, :summary, :dt)
                ON CONFLICT DO NOTHING
            """), {
                "title":   f"{target_date} 市場情報每日彙整",
                "summary": digest_text[:2000],
                "dt":      target_date,
            })

        logger.info(f"  每日彙整完成（{len(digest_text)} 字）")
        return digest_text

    except Exception as e:
        logger.error(f"  每日彙整失敗: {e}")
        return None
