"""
agent/pipeline_lock.py

Pipeline 互斥鎖（規格 SPEC_REASONING_LAYER.md P0-1）。

背景：2026-07-13 排程(GitHub Actions)與本機手動 pipeline 同時執行，造成
(1) 手動 run 的候選股法人資料在對方 backfill 途中被讀成全 0，辯論建立在污染輸入上；
(2) daily_recommendations 被兩組推薦交錯寫成 9 列 rank 重複；(3) 使用者收到兩則
不同選股的 Telegram。本模組用**現有的空表 pipeline_logs** 當鎖：

    開跑 → acquire_pipeline_lock()：搶佔一筆 status='running' 的列
           （partial unique index 保證同時只有一人搶到）
    結束 → release_pipeline_lock()：改成 success/failed
    逾時 → 超過 LOCK_TIMEOUT_MIN 的 running 列視為死鎖殘留，自動標 failed 後重搶

status 只能是 running/success/failed（表上有 CHECK 約束），逾時解除用 failed+
error_msg 註記，不新增狀態值。
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from database.connection import get_session

LOCK_TASK = "pipeline_lock"
LOCK_TIMEOUT_MIN = 90     # 超過 90 分鐘的 running 視為前次崩潰殘留（正常一輪 <20 分）


def ensure_lock_index():
    """partial unique index：同一時間最多一筆 running 的 pipeline_lock（搶鎖的原子性來源）。"""
    with get_session() as s:
        s.execute(text(f"""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_lock_running
            ON pipeline_logs (task_name) WHERE task_name = '{LOCK_TASK}' AND status = 'running'
        """))


def acquire_pipeline_lock(holder: str = "") -> int | None:
    """
    嘗試搶鎖。成功回傳鎖列 id；已有人在跑回傳 None（呼叫端應跳過本次執行）。
    原子性由 partial unique index 保證：同時兩個 INSERT 只會有一個成功，
    輸的那個收到 unique violation。逾時殘留的鎖自動標記 failed 後重試一次。
    """
    ensure_lock_index()
    for attempt in (1, 2):
        with get_session() as s:
            # 清掉逾時殘留（上次 pipeline 崩潰沒 release）
            s.execute(text(f"""
                UPDATE pipeline_logs
                SET status = 'failed',
                    finished_at = now(),
                    error_msg = '互斥鎖逾時自動解除（>{LOCK_TIMEOUT_MIN}分，前次執行可能崩潰）'
                WHERE task_name = :t AND status = 'running'
                  AND started_at < now() - interval '{LOCK_TIMEOUT_MIN} minutes'
            """), {"t": LOCK_TASK})
        try:
            with get_session() as s:
                row = s.execute(text("""
                    INSERT INTO pipeline_logs (task_name, status, started_at, error_msg)
                    VALUES (:t, 'running', now(), :h)
                    RETURNING id
                """), {"t": LOCK_TASK, "h": holder or None}).fetchone()
            logger.info(f"🔒 已取得 pipeline 互斥鎖（id={row[0]}）")
            return int(row[0])
        except Exception as e:
            if "uq_pipeline_lock_running" not in str(e):
                raise             # 不是搶輸鎖，是真的壞了——照常往外丟
            if attempt == 1:
                continue          # 可能剛好撞上逾時鎖清理，再試一次
    logger.warning("⏳ 另一個 pipeline 正在執行（互斥鎖被持有），本次跳過以避免並發寫入污染")
    return None


def release_pipeline_lock(lock_id: int, success: bool = True, error: str = ""):
    """釋放鎖。放在 finally 呼叫；失敗不擋流程（逾時機制會兜底）。"""
    try:
        with get_session() as s:
            s.execute(text("""
                UPDATE pipeline_logs
                SET status = :st, finished_at = now(),
                    error_msg = COALESCE(NULLIF(:err, ''), error_msg)
                WHERE id = :i AND status = 'running'
            """), {"st": "success" if success else "failed",
                   "err": (error or "")[:500], "i": lock_id})
        logger.info(f"🔓 已釋放 pipeline 互斥鎖（id={lock_id}）")
    except Exception as e:
        logger.warning(f"釋放互斥鎖失敗（逾時機制會自動兜底）: {e}")
