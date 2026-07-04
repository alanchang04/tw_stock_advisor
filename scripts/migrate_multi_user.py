"""
scripts/migrate_multi_user.py — Migration 12 執行器（跑一次即可，冪等）

做四件事：
  1. 執行 12_multi_user.sql 建表
  2. 建立管理員帳號（預設 alan，密碼可用 --password 指定）
  3. 舊 user_watchlist 資料 → alan 的「預設清單」
  4. 既有手動持倉歸戶給 alan

跑法：py -3.12 scripts/migrate_multi_user.py --password "你的密碼"
"""
import sys, os, io, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from sqlalchemy import text
from database.connection import get_session
from database.users import create_user, list_users


def main(admin_user: str, admin_pass: str):
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "database", "migrations", "12_multi_user.sql")
    with get_session() as s:
        s.execute(text(open(base, encoding="utf-8").read()))
    print("1) 建表 OK")

    ok, msg = create_user(admin_user, admin_pass, display_name=admin_user, role="admin")
    print(f"2) {msg}")

    with get_session() as s:
        uid = s.execute(text("SELECT user_id FROM users WHERE username = :u"),
                        {"u": admin_user}).scalar()
        lid = s.execute(text("""
            SELECT list_id FROM watchlists WHERE user_id = :uid ORDER BY list_id LIMIT 1
        """), {"uid": uid}).scalar()

        # 3) 舊 user_watchlist → 預設清單（表可能不存在於全新部署）
        moved = 0
        try:
            moved = s.execute(text("""
                INSERT INTO watchlist_items
                    (list_id, stock_id, note, target_price, last_signal, signal_date, added_date)
                SELECT :lid, stock_id, note, target_price, last_signal, signal_date, added_date
                FROM user_watchlist
                ON CONFLICT (list_id, stock_id) DO NOTHING
            """), {"lid": lid}).rowcount
        except Exception as e:
            print(f"   （舊 user_watchlist 無資料或不存在：{str(e)[:60]}）")
        print(f"3) 舊追蹤清單搬移 {moved} 筆 → {admin_user} 的預設清單")

        # 4) 手動持倉歸戶
        n = s.execute(text("""
            UPDATE positions SET user_id = :uid
            WHERE source = 'manual' AND user_id IS NULL
        """), {"uid": uid}).rowcount
        print(f"4) 手動持倉歸戶 {n} 筆 → {admin_user}")

    print("\n目前使用者：")
    for u in list_users():
        print(f"  #{u['user_id']} {u['username']} ({u['role']}) tg={u['telegram_chat_id'] or '-'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="alan")
    ap.add_argument("--password", required=True)
    a = ap.parse_args()
    main(a.user, a.password)
