"""
config.py — 集中管理系統設定
從 .env 讀取，所有模組 import 這個檔即可
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# 台灣時區（GitHub Actions 跑在 UTC，date.today() 會差 8 小時；
# 所有「訊號日期/資料日期」判斷一律用 tw_today()）
TW_TZ = ZoneInfo("Asia/Taipei")


def tw_today():
    return datetime.now(TW_TZ).date()


class DBConfig:
    HOST     = os.getenv("DB_HOST", "localhost")
    PORT     = int(os.getenv("DB_PORT", 5432))
    NAME     = os.getenv("DB_NAME", "taiwan_stock")
    USER     = os.getenv("DB_USER", "stock_user")
    PASSWORD = os.getenv("DB_PASSWORD", "stock_pass")

    @classmethod
    def url(cls) -> str:
        # 優先使用完整 DATABASE_URL（Streamlit Cloud Secrets / Neon 格式）
        raw = os.getenv("DATABASE_URL", "")
        if raw:
            # 確保使用 psycopg3 dialect
            if raw.startswith("postgres://"):
                raw = raw.replace("postgres://", "postgresql+psycopg://", 1)
            elif raw.startswith("postgresql://"):
                raw = raw.replace("postgresql://", "postgresql+psycopg://", 1)
            return raw
        # 本機 Docker 個別變數 fallback
        return (
            f"postgresql+psycopg://{cls.USER}:{cls.PASSWORD}"
            f"@{cls.HOST}:{cls.PORT}/{cls.NAME}"
        )


class APIConfig:
    FINMIND_TOKEN    = os.getenv("FINMIND_TOKEN", "")
    ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
    GEMINI_KEY       = os.getenv("GEMINI_API_KEY", "")
    GEMINI_API_KEY   = GEMINI_KEY   # 別名：scrapers/analysis 模組以此名稱引用
    # gemini-1.5-flash 已被 Google 下架；集中管理模型名稱，未來換版只改這一行
    GEMINI_MODEL     = os.getenv("GEMINI_MODEL", "gemini/gemini-2.5-flash")
    TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


class ScraperConfig:
    DELAY_SEC       = float(os.getenv("SCRAPER_DELAY_SEC", 2))
    TIMEOUT_SEC     = int(os.getenv("REQUEST_TIMEOUT_SEC", 30))
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }


class ScheduleConfig:
    DAILY_FETCH_HOUR    = int(os.getenv("DAILY_FETCH_HOUR", 18))
    DAILY_ANALYSIS_HOUR = int(os.getenv("DAILY_ANALYSIS_HOUR", 19))


# 預設追蹤的 YouTube 頻道（頻道 ID）
YOUTUBE_CHANNELS = [
    # 範例，之後再補你想追蹤的頻道
    # "UC_xxxxxxxxxxxx",
]