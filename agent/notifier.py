"""
agent/notifier.py

Telegram 通知：每日流程完成時推播推薦結果，失敗時推播錯誤。
未設定 TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 時自動略過（不影響主流程）。

設定方式：
  1. Telegram 搜尋 @BotFather → /newbot → 取得 bot token
  2. 跟你的新 bot 傳一則訊息，然後開
     https://api.telegram.org/bot<TOKEN>/getUpdates 取得 chat.id
  3. 把兩者填入 .env：
       TELEGRAM_BOT_TOKEN=...
       TELEGRAM_CHAT_ID=...
"""
import requests
from loguru import logger

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config.settings import APIConfig

_TG_LIMIT = 4000   # Telegram 單則訊息上限約 4096 字，留點餘裕


def telegram_enabled() -> bool:
    return bool(APIConfig.TELEGRAM_TOKEN and APIConfig.TELEGRAM_CHAT_ID)


def send_telegram(text: str) -> bool:
    """送一則 Telegram 訊息；未設定或失敗時回 False（不丟例外）。"""
    if not telegram_enabled():
        logger.warning("未設定 Telegram（TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID），略過通知")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{APIConfig.TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": APIConfig.TELEGRAM_CHAT_ID,
                "text": text[:_TG_LIMIT],
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        resp.raise_for_status()
        logger.info("✅ Telegram 通知已送出")
        return True
    except Exception as e:
        logger.error(f"Telegram 通知失敗: {e}")
        return False


def notify_success(report: str):
    send_telegram("✅ 台股每日推薦完成\n\n" + (report or "（今日無推薦資料）"))


def notify_failure(stage: str, err: str):
    send_telegram(f"❌ 台股每日流程失敗\n階段：{stage}\n錯誤：{err}")


if __name__ == "__main__":
    # 手動測試：python agent/notifier.py
    ok = send_telegram("🔔 台股顧問系統測試訊息：Telegram 通知設定成功！")
    print("已設定且送出成功" if ok else "未設定或送出失敗（見上方日誌）")
