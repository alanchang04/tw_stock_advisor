"""
P0-1 pipeline 互斥鎖整合測試（真 DB；建立的鎖列以 id 追蹤、結束時清除）。
無法連 DB 時整檔 skip。若正式 pipeline 恰在執行（已有 running 鎖）也 skip，不干擾。
"""
import os
import sys

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import pipeline_lock as pl


@pytest.fixture
def db():
    try:
        from database.connection import get_session
        with get_session() as s:
            running = s.execute(text(
                "SELECT COUNT(*) FROM pipeline_logs WHERE task_name=:t AND status='running'"
            ), {"t": pl.LOCK_TASK}).scalar()
    except Exception as e:
        pytest.skip(f"DB 無法連線：{e}")
    if running:
        pytest.skip("正式 pipeline 正在執行（鎖被持有），跳過鎖測試以免干擾")
    created: list[int] = []
    yield created
    from database.connection import get_session
    with get_session() as s:
        for _id in created:
            s.execute(text("DELETE FROM pipeline_logs WHERE id=:i"), {"i": _id})


def test_lock_mutual_exclusion_and_release(db):
    lid = pl.acquire_pipeline_lock(holder="test-lock")
    assert lid is not None
    db.append(lid)

    assert pl.acquire_pipeline_lock(holder="test-lock-2") is None   # 第二人搶不到

    pl.release_pipeline_lock(lid, success=True)
    lid2 = pl.acquire_pipeline_lock(holder="test-lock-3")           # 釋放後可再搶
    assert lid2 is not None and lid2 != lid
    db.append(lid2)
    pl.release_pipeline_lock(lid2, success=False, error="test done")

    from database.connection import get_session
    with get_session() as s:
        st1, st2 = (s.execute(text(
            "SELECT status FROM pipeline_logs WHERE id=:i"), {"i": i}).scalar()
            for i in (lid, lid2))
    assert st1 == "success" and st2 == "failed"


def test_stale_lock_auto_expires(db):
    lid = pl.acquire_pipeline_lock(holder="test-stale")
    assert lid is not None
    db.append(lid)
    from database.connection import get_session
    with get_session() as s:                      # 假造成 2 小時前開始（>90 分逾時）
        s.execute(text(
            "UPDATE pipeline_logs SET started_at = now() - interval '2 hours' WHERE id=:i"
        ), {"i": lid})

    lid2 = pl.acquire_pipeline_lock(holder="test-stale-2")
    assert lid2 is not None                       # 逾時鎖被自動解除後搶到
    db.append(lid2)
    pl.release_pipeline_lock(lid2)

    with get_session() as s:
        st, err = s.execute(text(
            "SELECT status, error_msg FROM pipeline_logs WHERE id=:i"), {"i": lid}).fetchone()
    assert st == "failed" and "逾時" in (err or "")
