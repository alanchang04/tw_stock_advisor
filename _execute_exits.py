"""
手動執行今日出場：5201 凱衛、5210 寶碩、3029 零壹
"""
import sys
from datetime import date
sys.path.insert(0, '.')
from database.connection import get_session
from sqlalchemy import text
from agent.notifier import send_telegram

today = date.today()

exits = [
    {"stock_id": "5201", "exit_price": 28.80, "entry_price": 30.55, "reason": "跌破月線(MA20)"},
    {"stock_id": "5210", "exit_price": 30.00, "entry_price": 27.80, "reason": "跌破週線(MA5)"},
    {"stock_id": "3029", "exit_price": 103.50, "entry_price": 99.10, "reason": "跌破週線(MA5)"},
]

lines = [f"🔔 手動執行出場（{today}）："]

with get_session() as s:
    for e in exits:
        sid = e["stock_id"]
        px  = e["exit_price"]
        ep  = e["entry_price"]
        ret = round((px / ep - 1) * 100, 4)

        # 確認目前是 open
        row = s.execute(text(
            "SELECT id, peak_price FROM positions WHERE stock_id=:sid AND status='open'"
        ), {"sid": sid}).fetchone()

        if not row:
            print(f"{sid}: 找不到 open 部位，跳過")
            continue

        s.execute(text("""
            UPDATE positions
            SET status='closed', exit_date=:d, exit_price=:px,
                exit_reason=:r, return_pct=:ret
            WHERE stock_id=:sid AND status='open'
        """), {"d": today, "px": px, "r": e["reason"], "ret": ret, "sid": sid})

        sign = "+" if ret >= 0 else ""
        print(f"✅ {sid} 出場 {ep:.2f}→{px:.2f}  {sign}{ret:.1f}%  ({e['reason']})")
        lines.append(f"  {sid}：{sign}{ret:.1f}%（{e['reason']}，進{ep:.2f}→出{px:.2f}）")

# 送 Telegram
msg = "\n".join(lines)
send_telegram(msg)
print("\nTelegram 通知已送出")
