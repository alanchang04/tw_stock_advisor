"""
database/users.py — 使用者認證與管理

密碼以 pbkdf2_sha256(20萬輪) + 隨機鹽儲存，格式 "salt_hex$hash_hex"，不存明文。
"""
from __future__ import annotations
import hashlib
import os as _os
import secrets

from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from database.connection import get_session

_ITER = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITER)
    return f"{salt}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITER)
        return secrets.compare_digest(calc.hex(), h)
    except Exception:
        return False


def authenticate(username: str, password: str) -> dict | None:
    """成功回傳 {user_id, username, display_name, role}，失敗 None。"""
    with get_session() as s:
        r = s.execute(text("""
            SELECT user_id, username, password_hash, display_name, role
            FROM users WHERE username = :u
        """), {"u": username.strip()}).fetchone()
    if r and verify_password(password, r[2]):
        return {"user_id": r[0], "username": r[1],
                "display_name": r[3] or r[1], "role": r[4]}
    return None


def create_user(username: str, password: str, display_name: str = None,
                role: str = "user", telegram_chat_id: str = None) -> tuple[bool, str]:
    username = username.strip()
    if not username or not password:
        return False, "帳號與密碼不可為空"
    if role not in ("admin", "user"):
        return False, "角色只能是 admin 或 user"
    with get_session() as s:
        dup = s.execute(text("SELECT 1 FROM users WHERE username = :u"),
                        {"u": username}).fetchone()
        if dup:
            return False, f"帳號 {username} 已存在"
        s.execute(text("""
            INSERT INTO users (username, password_hash, display_name, role, telegram_chat_id)
            VALUES (:u, :p, :d, :r, :tg)
        """), {"u": username, "p": hash_password(password),
               "d": display_name or username, "r": role,
               "tg": telegram_chat_id or None})
        uid = s.execute(text("SELECT user_id FROM users WHERE username = :u"),
                        {"u": username}).scalar()
        # 每個新使用者都有一組預設清單
        s.execute(text("""
            INSERT INTO watchlists (user_id, list_name) VALUES (:uid, '預設清單')
            ON CONFLICT DO NOTHING
        """), {"uid": uid})
    return True, f"已建立使用者 {username}"


def list_users() -> list[dict]:
    with get_session() as s:
        rows = s.execute(text("""
            SELECT user_id, username, display_name, role, telegram_chat_id, created_at
            FROM users ORDER BY user_id
        """)).fetchall()
    return [dict(user_id=r[0], username=r[1], display_name=r[2],
                 role=r[3], telegram_chat_id=r[4], created_at=r[5]) for r in rows]


def set_password(username: str, new_password: str) -> bool:
    with get_session() as s:
        n = s.execute(text("UPDATE users SET password_hash = :p WHERE username = :u"),
                      {"p": hash_password(new_password), "u": username.strip()}).rowcount
    return n > 0


def set_telegram_chat(username: str, chat_id: str) -> bool:
    with get_session() as s:
        n = s.execute(text("UPDATE users SET telegram_chat_id = :c WHERE username = :u"),
                      {"c": chat_id.strip() or None, "u": username.strip()}).rowcount
    return n > 0


def get_user_by_chat(chat_id: str) -> dict | None:
    """Telegram chat_id → 使用者（Bot 指令歸戶用）。"""
    with get_session() as s:
        r = s.execute(text("""
            SELECT user_id, username, display_name, role FROM users
            WHERE telegram_chat_id = :c LIMIT 1
        """), {"c": str(chat_id)}).fetchone()
    return ({"user_id": r[0], "username": r[1],
             "display_name": r[2] or r[1], "role": r[3]} if r else None)


def default_list_id(user_id: int) -> int:
    """使用者的預設清單 id（沒有就建立）。"""
    with get_session() as s:
        r = s.execute(text("""
            SELECT list_id FROM watchlists
            WHERE user_id = :uid ORDER BY list_id LIMIT 1
        """), {"uid": user_id}).fetchone()
        if r:
            return r[0]
        s.execute(text("INSERT INTO watchlists (user_id, list_name) VALUES (:uid, '預設清單')"),
                  {"uid": user_id})
        return s.execute(text("""
            SELECT list_id FROM watchlists WHERE user_id = :uid ORDER BY list_id LIMIT 1
        """), {"uid": user_id}).scalar()
