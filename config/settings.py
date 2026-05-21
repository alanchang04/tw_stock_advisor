"""
config.py — 集中管理系統設定
從 .env 讀取，所有模組 import 這個檔即可
"""
import os
from dotenv import load_dotenv

load_dotenv()


class DBConfig:
    HOST     = os.getenv("DB_HOST", "localhost")
    PORT     = int(os.getenv("DB_PORT", 5432))
    NAME     = os.getenv("DB_NAME", "taiwan_stock")
    USER     = os.getenv("DB_USER", "stock_user")
    PASSWORD = os.getenv("DB_PASSWORD", "stock_pass")

    @classmethod
    def url(cls) -> str:
        return (
            f"postgresql+psycopg://{cls.USER}:{cls.PASSWORD}"
            f"@{cls.HOST}:{cls.PORT}/{cls.NAME}"
        )


class APIConfig:
    FINMIND_TOKEN    = os.getenv("FINMIND_TOKEN", "")
    ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
    GEMINI_KEY       = os.getenv("GEMINI_API_KEY", "")
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