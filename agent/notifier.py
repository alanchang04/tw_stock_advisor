"""
agent/notifier.py

Telegram 通知：每日流程完成時推播推薦結果，失敗時推播錯誤。
也支援簡單的互動指令（/help、/status、/positions），
透過 check_and_respond() 在每次 pipeline 開始時處理排隊中的訊息。

設定方式：
  1. Telegram 搜尋 @BotFather → /newbot → 取得 bot token
  2. 跟你的新 bot 傳一則訊息，然後開
     https://api.telegram.org/bot<TOKEN>/getUpdates 取得 chat.id
  3. 把兩者填入 .env：
       TELEGRAM_BOT_TOKEN=...
       TELEGRAM_CHAT_ID=...
"""
import json
import os
import requests
from loguru import logger

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config.settings import APIConfig

_TG_LIMIT = 4000   # Telegram 單則訊息上限約 4096 字，留點餘裕
_STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".telegram_state.json")


# ══════════════════════════════════════════════════════════════════
#  基本傳訊
# ══════════════════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════════════════
#  Bot 互動指令
# ══════════════════════════════════════════════════════════════════
def _load_offset() -> int:
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("offset", 0)
    except Exception:
        return 0


def _save_offset(offset: int):
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"offset": offset}, f)
    except Exception as e:
        logger.warning(f"無法儲存 telegram offset: {e}")


def get_updates() -> list[dict]:
    """從 Telegram 拉取未讀訊息（getUpdates long-poll，timeout=0 表示立即返回）。"""
    if not telegram_enabled():
        return []
    offset = _load_offset()
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{APIConfig.TELEGRAM_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 0, "limit": 20},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        updates = data.get("result", [])
        if updates:
            _save_offset(updates[-1]["update_id"] + 1)
        return updates
    except Exception as e:
        logger.warning(f"getUpdates 失敗: {e}")
        return []


def _handle_command(cmd: str) -> str:
    """把指令文字轉成回覆文字。"""
    cmd = cmd.strip().lower().split()[0]  # 取第一個 token，忽略 @botname 後綴

    if cmd in ("/help", "/start"):
        return (
            "📋 台股顧問 Bot 指令：\n"
            "/status    — 系統狀態與今日日期\n"
            "/positions — 目前追蹤中的持倉\n"
            "/help      — 顯示此說明\n\n"
            "每日 21:00 自動推送選股推薦與出場提醒。"
        )

    if cmd == "/status":
        from datetime import date
        import sys, os
        # 嘗試連 DB
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
            from database.connection import get_session
            from sqlalchemy import text
            with get_session() as s:
                cnt = s.execute(text("SELECT COUNT(*) FROM daily_prices")).scalar()
            db_line = f"✅ DB 連線正常，daily_prices 共 {cnt:,} 筆"
        except Exception as e:
            db_line = f"❌ DB 連線失敗：{e}"
        return (
            f"🖥 台股顧問系統狀態\n"
            f"日期：{date.today()}\n"
            f"{db_line}"
        )

    if cmd == "/positions":
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
            from agent.portfolio import open_positions
            held = open_positions()
            if not held:
                return "📦 目前無追蹤中部位"
            lines = [f"📦 追蹤中部位（{len(held)} 檔）："]
            for p in held:
                gain_ref = f"進場 {p['entry_price']:.2f}"
                lines.append(f"  {p['stock_id']} {p['stock_name']} — {gain_ref}，自 {p['entry_date']}")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 無法取得部位資料：{e}"

    return f"❓ 未知指令：{cmd}，輸入 /help 查看可用指令"


def check_and_respond():
    """
    在 pipeline 開始時呼叫：處理用戶傳給 Bot 的所有排隊指令並回覆。
    只處理來自已設定 TELEGRAM_CHAT_ID 的訊息（安全過濾）。
    """
    if not telegram_enabled():
        return
    updates = get_updates()
    for upd in updates:
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(APIConfig.TELEGRAM_CHAT_ID):
            continue   # 只回應已授權的 chat
        text_raw = msg.get("text", "")
        if not text_raw.startswith("/"):
            continue   # 只處理指令
        reply = _handle_command(text_raw)
        send_telegram(reply)
        logger.info(f"已回覆指令：{text_raw.split()[0]}")


if __name__ == "__main__":
    # 手動測試：python agent/notifier.py
    ok = send_telegram("🔔 台股顧問系統測試訊息：Telegram 通知設定成功！")
    print("已設定且送出成功" if ok else "未設定或送出失敗（見上方日誌）")
