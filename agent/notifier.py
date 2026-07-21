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


def send_telegram(text: str, chat_id: str = None) -> bool:
    """送一則 Telegram 訊息；chat_id 未指定時送到預設（管理員）。失敗回 False。"""
    if not telegram_enabled():
        logger.warning("未設定 Telegram（TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID），略過通知")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{APIConfig.TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id or APIConfig.TELEGRAM_CHAT_ID,
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
#  Bot 指令選單（Telegram 原生「/」選單，點了就看得到全部指令與說明）
# ══════════════════════════════════════════════════════════════════
_BOT_COMMANDS = [
    ("help",       "查看所有指令說明"),
    ("status",     "系統狀態（資料更新到哪天）"),
    ("stock",      "查詢個股即時狀態，例：/stock 2330"),
    ("digest",     "今日市場情報彙整（族群/風險/氛圍）"),
    ("recommend",  "今日 AI 選股推薦"),
    ("smartmoney", "聰明資金重點（投信連買/統一ETF換股）"),
    ("sector",     "族群輪動排行（含龍頭股）"),
    ("etf",        "近期 ETF 換股記錄"),
    ("positions",  "我的持倉（AI部位 + 手動記錄）"),
    ("watchlist",  "我的追蹤清單與買點訊號"),
    ("watch",      "加入追蹤，例：/watch 2330 900"),
    ("unwatch",    "移除追蹤，例：/unwatch 2330"),
    ("buy",        "記錄進場，例：/buy 2330 950 2"),
    ("sell",       "記錄平倉，例：/sell 2330 1010"),
]


def register_bot_commands() -> bool:
    """
    向 Telegram 註冊指令選單（setMyCommands）——註冊後使用者在聊天室點「/」
    會直接跳出全部指令＋一行說明，不用死記或猜。冪等，可重複呼叫。
    """
    if not telegram_enabled():
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{APIConfig.TELEGRAM_TOKEN}/setMyCommands",
            json={"commands": [{"command": c, "description": d} for c, d in _BOT_COMMANDS]},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"註冊 Bot 指令選單失敗: {e}")
        return False


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


import re as _re

_CODE_RE = _re.compile(r'(?<![\dA-Z])(\d{4,6}[A-Z]?)(?![\dA-Z])')


def _stock_name(sid: str) -> str | None:
    """代號 → 名稱；不存在回 None。"""
    from database.connection import get_session
    from sqlalchemy import text
    with get_session() as s:
        r = s.execute(text("SELECT stock_name FROM stocks WHERE stock_id = :sid"),
                      {"sid": sid}).fetchone()
    return r[0] if r else None


def _add_watch(sid: str, target: float = None, note: str = None, user: dict = None) -> str:
    from database.connection import get_session
    from database.users import default_list_id
    from sqlalchemy import text
    name = _stock_name(sid)
    if not name:
        return f"❌ 代號 {sid} 不存在，請確認"
    lid = default_list_id(user["user_id"])
    with get_session() as s:
        s.execute(text("""
            INSERT INTO watchlist_items (list_id, stock_id, note, target_price)
            VALUES (:lid, :sid, :note, :tp)
            ON CONFLICT (list_id, stock_id) DO UPDATE
                SET note = COALESCE(EXCLUDED.note, watchlist_items.note),
                    target_price = COALESCE(EXCLUDED.target_price, watchlist_items.target_price)
        """), {"lid": lid, "sid": sid, "note": note, "tp": target})
    tp_txt = f"，目標價 {target:.2f}" if target else ""
    return (f"🔖 已加入 {user['display_name']} 的預設清單：{sid} {name}{tp_txt}\n"
            f"每日 21:00 會判斷買點並在出現 🟢 訊號時通知")


def _remove_watch(sid: str, user: dict = None) -> str:
    from database.connection import get_session
    from sqlalchemy import text
    with get_session() as s:
        n = s.execute(text("""
            DELETE FROM watchlist_items
            WHERE stock_id = :sid
              AND list_id IN (SELECT list_id FROM watchlists WHERE user_id = :uid)
        """), {"sid": sid, "uid": user["user_id"]}).rowcount
    return f"🗑 已從你的清單移除：{sid}" if n else f"❓ {sid} 不在你的任何清單中"


def _list_watch(user: dict = None) -> str:
    from database.connection import get_session
    from sqlalchemy import text
    with get_session() as s:
        rows = s.execute(text("""
            SELECT w.list_name, i.stock_id, st.stock_name, i.target_price, i.last_signal
            FROM watchlist_items i
            JOIN watchlists w ON w.list_id = i.list_id
            JOIN stocks st    ON st.stock_id = i.stock_id
            WHERE w.user_id = :uid
            ORDER BY w.list_id, i.stock_id
        """), {"uid": user["user_id"]}).fetchall()
    if not rows:
        return "🔖 你的追蹤清單是空的\n用「/watch 2330」或直接說「幫我關注 2330」加入"
    lines = [f"🔖 {user['display_name']} 的追蹤清單（{len(rows)} 檔）："]
    cur = None
    for lname, sid, name, tp, sig in rows:
        if lname != cur:
            lines.append(f"📁 {lname}")
            cur = lname
        tp_txt = f"｜目標 {float(tp):.2f}" if tp else ""
        sig_txt = f"\n    {sig}" if sig else ""
        lines.append(f"  {sid} {name}{tp_txt}{sig_txt}")
    return "\n".join(lines)


def _add_manual(sid: str, price: float, lots: int, note: str = None, user: dict = None) -> str:
    """新增手動持倉（歸戶到 user）。lots = 張數（1張=1000股）。"""
    from datetime import date
    from database.connection import get_session
    from sqlalchemy import text
    name = _stock_name(sid)
    if not name:
        return f"❌ 代號 {sid} 不存在，請確認"
    if price <= 0 or lots <= 0:
        return "❌ 價格與張數必須大於 0"
    with get_session() as s:
        s.execute(text("""
            INSERT INTO positions (stock_id, entry_date, entry_price, shares,
                                   entry_reason, source, status, user_id, account_label)
            VALUES (:sid, :d, :p, :sh, :note, 'manual', 'open', :uid, '我的')
        """), {"sid": sid, "d": date.today(), "p": price,
               "sh": lots * 1000, "note": note or "Telegram 建倉",
               "uid": user["user_id"]})
    return (f"📦 已記錄進場（{user['display_name']}）：{sid} {name} @ {price:.2f} × {lots} 張\n"
            f"每日 21:00 會給出建議（賣出/加碼/續抱）")


def _close_manual(sid: str, price: float, user: dict = None) -> str:
    """平掉該使用者某代號最早一筆 open 手動倉。"""
    from datetime import date
    from database.connection import get_session
    from sqlalchemy import text
    with get_session() as s:
        row = s.execute(text("""
            SELECT id, entry_price FROM positions
            WHERE stock_id = :sid AND source = 'manual' AND status = 'open'
              AND user_id = :uid
            ORDER BY entry_date LIMIT 1
        """), {"sid": sid, "uid": user["user_id"]}).fetchone()
        if not row:
            return f"❓ 你沒有 {sid} 進行中的手動持倉"
        pid, ep = row[0], float(row[1])
        ret = (price / ep - 1) * 100
        s.execute(text("""
            UPDATE positions SET status='closed', exit_date=:d, exit_price=:p,
                   exit_reason='Telegram 手動平倉', return_pct=:r
            WHERE id = :pid
        """), {"d": date.today(), "p": price, "r": round(ret, 4), "pid": pid})
    name = _stock_name(sid) or ""
    emoji = "🟢" if ret >= 0 else "🔴"
    return f"{emoji} 已平倉：{sid} {name} @ {price:.2f}（成本 {ep:.2f}，報酬 {ret:+.2f}%）"


def _my_positions(user: dict = None) -> str:
    """AI 部位（全系統共享）+ 該使用者的手動持倉（含最近 AI 建議與帳本）。"""
    from database.connection import get_session
    from sqlalchemy import text
    lines = []
    try:
        from agent.portfolio import open_positions
        held = open_positions()
        lines.append(f"🤖 AI 部位（{len(held)} 檔）：" if held else "🤖 AI 部位：無")
        for p in held:
            lines.append(f"  {p['stock_id']} {p['stock_name']} — 進場 {p['entry_price']:.2f}，自 {p['entry_date']}")
        with get_session() as s:
            rows = s.execute(text("""
                SELECT p.stock_id, st.stock_name, p.entry_price, p.shares, p.last_advice,
                       COALESCE(p.account_label, '我的'),
                       (SELECT d.close FROM daily_prices d
                        WHERE d.stock_id = p.stock_id AND d.close > 0
                        ORDER BY d.trade_date DESC LIMIT 1)
                FROM positions p JOIN stocks st ON st.stock_id = p.stock_id
                WHERE p.source = 'manual' AND p.status = 'open' AND p.user_id = :uid
                ORDER BY p.account_label, p.entry_date
            """), {"uid": user["user_id"]}).fetchall()
        lines.append(f"\n👤 {user['display_name']} 的持倉（{len(rows)} 檔）：" if rows
                     else f"\n👤 {user['display_name']} 的持倉：無")
        for sid, name, ep, sh, adv, label, cur in rows:
            ep = float(ep); cur = float(cur) if cur else None
            pnl = f"{(cur/ep-1)*100:+.1f}%" if cur else "—"
            if sh:
                sh = int(sh)
                lots = f"{sh//1000}張" if sh >= 1000 else f"{sh}股"
            else:
                lots = ""
            lbl = f"[{label}] " if label != "我的" else ""
            lines.append(f"  {lbl}{sid} {name} {lots} @ {ep:.2f}（{pnl}）")
            if adv:
                lines.append(f"    {adv}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 無法取得部位資料：{e}"


def _stock_snapshot(sid: str, user: dict) -> str:
    """個股即時狀態：價格/技術訊號/近5日籌碼 + 是否在自己的清單或持倉中。"""
    from database.connection import get_session
    from sqlalchemy import text
    name = _stock_name(sid)
    if not name:
        return f"❌ 代號 {sid} 不存在，請確認"

    with get_session() as s:
        row = s.execute(text("""
            SELECT p.trade_date, p.close, p.change_pct,
                   t.rsi14, t.macd_hist, t.signal_ma_cross, t.signal_breakout
            FROM daily_prices p
            LEFT JOIN technical_indicators t
                   ON t.stock_id = p.stock_id AND t.trade_date = p.trade_date
            WHERE p.stock_id = :sid AND p.close > 0
            ORDER BY p.trade_date DESC LIMIT 1
        """), {"sid": sid}).fetchone()
        if row is None:
            return f"❌ {sid} {name} 尚無價格資料"

        inst5 = s.execute(text("""
            SELECT COALESCE(SUM(total_net),0), COALESCE(SUM(invest_net),0)
            FROM (SELECT total_net, invest_net FROM institutional_trading
                  WHERE stock_id = :sid ORDER BY trade_date DESC LIMIT 5) t5
        """), {"sid": sid}).fetchone()

        in_watch = s.execute(text("""
            SELECT w.list_name FROM watchlist_items i
            JOIN watchlists w ON w.list_id = i.list_id
            WHERE w.user_id = :uid AND i.stock_id = :sid
        """), {"uid": user["user_id"], "sid": sid}).fetchall()

        in_pos = s.execute(text("""
            SELECT source FROM positions
            WHERE stock_id = :sid AND status = 'open' AND (source = 'ai' OR user_id = :uid)
        """), {"uid": user["user_id"], "sid": sid}).fetchall()

    trade_date, close, chg, rsi, macd_hist, ma_cross, breakout = row
    close, chg = float(close), float(chg or 0)
    total5, invest5 = float(inst5[0] or 0) / 1000, float(inst5[1] or 0) / 1000

    lines = [f"📉 {sid} {name}　（{trade_date}）",
             f"收盤 {close:.2f}（{chg:+.2f}%）"]
    if rsi is not None:
        lines.append(f"RSI {float(rsi):.1f}　MACD柱 {'正' if (macd_hist or 0) > 0 else '負'}")
    ma_txt = {1: "黃金交叉", -1: "死亡交叉"}.get(int(ma_cross or 0), "無訊號")
    bo_txt = {1: "突破壓力", -1: "跌破支撐"}.get(int(breakout or 0), "無訊號")
    lines.append(f"均線：{ma_txt}　突破：{bo_txt}")
    lines.append(f"近5日：三大法人 {total5:+,.0f} 張｜投信 {invest5:+,.0f} 張")
    if in_watch:
        lines.append(f"🔖 在你的清單：{', '.join(r[0] for r in in_watch)}")
    if in_pos:
        tags = ["AI部位" if r[0] == "ai" else "你的持倉" for r in in_pos]
        lines.append(f"📦 持有中：{', '.join(tags)}")
    return "\n".join(lines)


def _today_digest() -> str:
    from data_pipeline.analysis.daily_digest import get_latest_digest, digest_age_days
    latest = get_latest_digest()
    if not latest:
        return "📋 近期尚無市場彙整（每日 21:00 pipeline 跑完後自動產生）"
    d, text_ = latest
    age = digest_age_days(d)
    stale = f"\n⚠️ 這是 {age} 天前的彙整，非今日最新（今日資料蒐集可能中斷）" if age > 0 else ""
    return f"📋 {d} 市場情報每日彙整{stale}\n\n{text_}"


def _today_recommend() -> str:
    from database.connection import get_session
    from sqlalchemy import text
    with get_session() as s:
        rec_date = s.execute(text("SELECT MAX(rec_date) FROM daily_recommendations")).scalar()
        if not rec_date:
            return "📊 尚無 AI 選股推薦紀錄"
        rows = s.execute(text("""
            SELECT r.rank, r.stock_id, st.stock_name, r.reason
            FROM daily_recommendations r JOIN stocks st ON st.stock_id = r.stock_id
            WHERE r.rec_date = :d ORDER BY r.rank
        """), {"d": rec_date}).fetchall()
    lines = [f"📊 {rec_date} AI 選股推薦（{len(rows)} 檔）："]
    for rank, sid, name, reason in rows:
        lines.append(f"#{rank} {sid} {name}")
        if reason:
            lines.append(f"   {reason[:80]}")
    return "\n".join(lines)


def _today_smart_money() -> str:
    from data_pipeline.analysis.smart_money import get_todays_highlights
    highlights = get_todays_highlights(limit=10)
    if not highlights:
        return "🧠 今日尚無聰明資金訊號（資料每日 21:00 更新）"
    return f"🧠 今日聰明資金重點：\n{highlights}"


def _today_sector() -> str:
    from data_pipeline.analysis.group_momentum import calc_group_momentum, rotation_alerts
    df = calc_group_momentum()
    if df.empty:
        return "🔥 尚無族群輪動資料"
    lines = [f"🔥 族群輪動排行（{df['trade_date'].iloc[0]}）："]
    for _, r in df.head(6).iterrows():
        lines.append(f"  {r['group_name']}　平均{r['avg_change_pct']:+.1f}%"
                     f"　龍頭{r['leader_names']}{r['leader_change_pct']:+.1f}%")
    alerts = rotation_alerts(df)
    if alerts:
        lines.append("\n" + "\n".join(alerts))
    return "\n".join(lines)


def _today_etf() -> str:
    from database.connection import get_session
    from sqlalchemy import text
    from data_pipeline.fetchers.uni_etf_fetcher import UNI_ACTIVE_FUNDS
    with get_session() as s:
        rows = s.execute(text("""
            SELECT ec.etf_id, ec.etf_name, ec.stock_id, ec.stock_name,
                   ec.change_type, ec.old_weight, ec.new_weight, ec.detected_date
            FROM etf_changes ec
            WHERE ec.detected_date >= CURRENT_DATE - 14 * INTERVAL '1 day'
            ORDER BY ec.detected_date DESC LIMIT 20
        """)).fetchall()
    if not rows:
        return "🔀 近14天無 ETF 換股記錄"
    label = {"added": "🆕新增", "removed": "🚫剔除", "increased": "⬆加碼", "decreased": "⬇減碼"}
    lines = ["🔀 近期 ETF 換股（統一主動式優先）："]
    uni, other = [], []
    for eid, ename, sid, sname, ctype, old_w, new_w, dd in rows:
        line = f"  [{eid} {ename}] {sid} {sname} {label.get(ctype, ctype)}（{float(old_w or 0):.1f}%→{float(new_w or 0):.1f}%）{dd}"
        (uni if eid in UNI_ACTIVE_FUNDS else other).append(line)
    lines += uni or []
    if other:
        lines.append(f"— 其他ETF（{len(other)}筆）—")
        lines += other[:10]
    return "\n".join(lines)


def _handle_command(text_raw: str, user: dict) -> str:
    """把指令文字轉成回覆文字。支援參數（如 /watch 2330 950）。user 為歸戶對象。"""
    parts = text_raw.strip().split()
    cmd = parts[0].lower().split("@")[0]
    args = parts[1:]

    if cmd in ("/help", "/start"):
        return (
            "📋 台股顧問 Bot 指令一覽\n"
            "（也可點輸入框旁的「/」按鈕看選單，隨時跳出說明）\n\n"
            "／查詢市場（隨時可問，不用等每日報告）\n"
            "/stock 2330 — 查個股價格/技術/籌碼即時狀態\n"
            "/digest — 今日市場情報彙整（族群/風險/氛圍）\n"
            "/recommend — 今日 AI 選股推薦\n"
            "/smartmoney — 聰明資金重點（投信連買/統一ETF換股）\n"
            "/sector — 族群輪動排行＋龍頭股\n"
            "/etf — 近14天 ETF 換股記錄\n\n"
            "／追蹤清單\n"
            "/watch 2330 [目標價] — 加入追蹤\n"
            "/unwatch 2330 — 移除追蹤\n"
            "/watchlist — 看清單與買點訊號\n\n"
            "／我的持倉\n"
            "/buy 2330 950 2 — 記錄進場（價格 950、2 張）\n"
            "/sell 2330 1010 — 記錄平倉\n"
            "/positions — AI 部位 + 我的持倉\n\n"
            "／系統\n"
            "/status — 系統狀態\n\n"
            "💬 也可以直接說：「幫我關注 2330」「我買了 2330 950 2張」「2330 我 1010 賣掉了」\n\n"
            "每日 21:00 自動推送推薦、持倉建議與追蹤買點；上面的查詢指令則是隨問隨查——"
            "但 Bot 只在每日 pipeline 執行時（21:00 或你按「立即更新」）處理排隊訊息，"
            "不是秒回，傳完稍等一下即可。"
        )

    if cmd == "/status":
        from datetime import date
        try:
            from database.connection import get_session
            from sqlalchemy import text
            with get_session() as s:
                cnt = s.execute(text("SELECT COUNT(*) FROM daily_prices")).scalar()
                last = s.execute(text("SELECT MAX(trade_date) FROM daily_prices")).scalar()
            db_line = f"✅ DB 正常，行情至 {last}（{cnt:,} 筆）"
        except Exception as e:
            db_line = f"❌ DB 連線失敗：{e}"
        return f"🖥 台股顧問系統狀態\n日期：{date.today()}\n{db_line}"

    if cmd == "/positions":
        return _my_positions(user)

    if cmd == "/watchlist":
        return _list_watch(user)

    if cmd == "/watch":
        if not args:
            return "用法：/watch 2330 [目標價]"
        target = float(args[1]) if len(args) > 1 and _re.fullmatch(r'\d+(\.\d+)?', args[1]) else None
        return _add_watch(args[0].upper(), target, user=user)

    if cmd == "/unwatch":
        if not args:
            return "用法：/unwatch 2330"
        return _remove_watch(args[0].upper(), user=user)

    if cmd == "/buy":
        if len(args) < 3:
            return "用法：/buy 代號 價格 張數\n例：/buy 2330 950 2"
        try:
            return _add_manual(args[0].upper(), float(args[1]), int(args[2]),
                               note=" ".join(args[3:]) or None, user=user)
        except ValueError:
            return "❌ 價格/張數格式錯誤。例：/buy 2330 950 2"

    if cmd == "/sell":
        if len(args) < 2:
            return "用法：/sell 代號 賣出價格\n例：/sell 2330 1010"
        try:
            return _close_manual(args[0].upper(), float(args[1]), user=user)
        except ValueError:
            return "❌ 價格格式錯誤。例：/sell 2330 1010"

    if cmd == "/stock":
        if not args:
            return "用法：/stock 2330"
        return _stock_snapshot(args[0].upper(), user)

    if cmd == "/digest":
        return _today_digest()

    if cmd == "/recommend":
        return _today_recommend()

    if cmd == "/smartmoney":
        return _today_smart_money()

    if cmd == "/sector":
        return _today_sector()

    if cmd == "/etf":
        return _today_etf()

    return f"❓ 未知指令：{cmd}\n輸入 /help 查看可用指令，或點輸入框旁的「/」按鈕看選單"


def _try_natural(text_raw: str, user: dict) -> str | None:
    """
    自然語言解析（規則式，零 LLM 成本）：
      「幫我關注 2330」「追蹤 2330 目標 900」        → watch
      「我買了 2330 950 2張」「2330 進場 950 兩張」   → buy（張數缺省 1）
      「2330 1010 賣掉了」「賣出 2330 1010」          → sell
      句中有股票代號但看不懂動作 → 提示 /stock 查詢與可用寫法（不留白讓人猜）
    完全沒有股票代號的雜訊訊息才回 None（不回覆，避免誤觸/洗版）。
    """
    t = text_raw.strip()
    m = _CODE_RE.search(t)
    if not m:
        return None
    sid = m.group(1).upper()
    rest = t.replace(m.group(1), " ")
    nums = [float(x) for x in _re.findall(r'\d+(?:\.\d+)?', rest)]

    # 唯讀查詢優先判斷（最安全，放最前面）
    if any(k in t for k in ("如何", "現在", "怎樣", "狀態", "近況", "查一下", "查詢")):
        return _stock_snapshot(sid, user)

    if any(k in t for k in ("賣", "平倉", "出場", "出掉")):
        if not nums:
            return f"要記錄 {sid} 平倉的話，請附上賣出價格，例：「{sid} 1010 賣掉了」"
        return _close_manual(sid, nums[0], user=user)

    if any(k in t for k in ("買了", "買入", "買進", "進場", "建倉")):
        if not nums:
            return f"要記錄 {sid} 進場的話，請附上價格（與張數），例：「我買了 {sid} 950 2張」"
        price = nums[0]
        lot_m = _re.search(r'(\d+)\s*張', rest)
        lots = int(lot_m.group(1)) if lot_m else (int(nums[1]) if len(nums) > 1 and nums[1] < 1000 else 1)
        return _add_manual(sid, price, lots, user=user)

    if any(k in t for k in ("關注", "追蹤", "注意", "看看", "觀察")):
        target = nums[0] if nums else None
        return _add_watch(sid, target, user=user)

    # 偵測到股票代號但看不懂想做什麼 → 給提示而非沉默，這是 Bot 好不好用的關鍵
    return (f"看到 {sid}，但不確定你想做什麼，可以試試：\n"
            f"「{sid} 現在如何」或 /stock {sid} — 查即時狀態\n"
            f"「幫我關注 {sid}」— 加入追蹤清單\n"
            f"「我買了 {sid} 價格 張數」— 記錄進場\n"
            f"「{sid} 價格 賣掉了」— 記錄平倉")


def _resolve_user(chat_id: str) -> dict | None:
    """
    chat_id → 使用者（多使用者歸戶）：
      1. users.telegram_chat_id 綁定者 → 該使用者
      2. 預設 TELEGRAM_CHAT_ID（env）→ 管理員（第一位 admin）
      3. 其他 → None（未授權，不回應）
    """
    try:
        from database.users import get_user_by_chat
        u = get_user_by_chat(chat_id)
        if u:
            return u
        if str(chat_id) == str(APIConfig.TELEGRAM_CHAT_ID):
            from database.connection import get_session
            from sqlalchemy import text as _text
            with get_session() as s:
                r = s.execute(_text("""
                    SELECT user_id, username, display_name, role FROM users
                    WHERE role = 'admin' ORDER BY user_id LIMIT 1
                """)).fetchone()
            if r:
                return {"user_id": r[0], "username": r[1],
                        "display_name": r[2] or r[1], "role": r[3]}
    except Exception as e:
        logger.warning(f"使用者解析失敗: {e}")
    return None


def check_and_respond():
    """
    在 pipeline 開始時呼叫：處理用戶傳給 Bot 的所有排隊指令並回覆。
    授權對象：env TELEGRAM_CHAT_ID（→管理員）+ users 表綁定 chat_id 的使用者。
    回覆送回發訊者自己的 chat。
    """
    if not telegram_enabled():
        return
    register_bot_commands()   # 確保 Telegram 的「/」選單隨時是最新指令列表（冪等，成本可忽略）
    updates = get_updates()
    for upd in updates:
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        chat_id = str(msg.get("chat", {}).get("id", ""))
        user = _resolve_user(chat_id)
        if user is None:
            continue   # 未授權的 chat 不回應
        text_raw = msg.get("text", "")
        if not text_raw:
            continue
        # 單則訊息處理失敗不可炸掉整條 pipeline（check_and_respond 在
        # mode_pipeline 的 try 區塊之前執行，未捕捉的例外會讓每日流程
        # 死在起跑點且連失敗通知都發不出去）
        try:
            if text_raw.startswith("/"):
                reply = _handle_command(text_raw, user)
            else:
                reply = _try_natural(text_raw, user)   # 自然語言：「幫我關注 2330」等
                if reply is None:
                    continue   # 看不懂就不回，避免誤觸
        except Exception as e:
            logger.error(f"處理訊息失敗（{text_raw[:30]}）: {e}")
            reply = "⚠️ 這個指令處理時發生錯誤，已記錄，請稍後再試或改用 /help 查看用法"
        send_telegram(reply, chat_id=chat_id)
        logger.info(f"已回覆 {user['username']}：{text_raw[:30]}")


if __name__ == "__main__":
    # 手動測試：python agent/notifier.py
    ok = send_telegram("🔔 台股顧問系統測試訊息：Telegram 通知設定成功！")
    print("已設定且送出成功" if ok else "未設定或送出失敗（見上方日誌）")
