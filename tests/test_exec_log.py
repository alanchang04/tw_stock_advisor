"""
決策軌跡 execution_log 測試（規格 Phase A 驗收）。

DB 整合測試沿用 test_portfolio_orders 的安全模式：get_session 換成共用、
永不 commit 的 session，結束 rollback——對真實 schema 驗證但不寫入任何資料。
"""
import json
import os
import sys
from contextlib import contextmanager

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import exec_log
from agent.exec_log import StageRec, _truncate_payload, MAX_PAYLOAD_BYTES


# ── 純函式（不需 DB）──────────────────────────────────────────────
def test_stage_is_noop_without_active_run():
    """沒 start_run 時（單元測試/腳本情境）stage 必須可用且不碰 DB。"""
    exec_log._current = None
    with exec_log.stage("anything") as rec:
        rec.summary = "no-op"
        rec.payload = {"x": 1}
    # 沒炸、沒 DB 連線需求，即通過


def test_stage_noop_does_not_swallow_exceptions():
    exec_log._current = None
    with pytest.raises(ValueError):
        with exec_log.stage("x"):
            raise ValueError("boom")


def test_payload_truncated_over_limit():
    big = {"text": "Ｘ" * 200_000}          # 遠超 100KB
    out = _truncate_payload(big)
    assert len(out.encode("utf-8")) <= MAX_PAYLOAD_BYTES + 1000  # 截斷後含註記
    parsed = json.loads(out)
    assert parsed["_truncated"] is True and parsed["_original_bytes"] > MAX_PAYLOAD_BYTES


def test_payload_small_passes_through():
    out = _truncate_payload({"a": 1, "日期": "2026-07-13"})
    assert json.loads(out) == {"a": 1, "日期": "2026-07-13"}


def test_unserializable_payload_degrades_gracefully():
    out = _truncate_payload({"bad": object()})   # default=str 會處理掉；不會丟例外
    assert out is not None


def test_add_llm_accepts_dict_and_none():
    rec = StageRec("s")
    rec.add_llm({"prompt_tokens": 100, "completion_tokens": 20})
    rec.add_llm(None)                            # 拿不到用量也要能記次數
    assert rec.model_calls == 2
    assert rec.tokens_in == 100 and rec.tokens_out == 20


# ── DB 整合（真實 schema，交易內回滾）─────────────────────────────
@pytest.fixture
def tx(monkeypatch):
    try:
        from database.connection import get_session_factory
        session = get_session_factory()()
        session.execute(text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"DB 無法連線，跳過整合測試：{e}")

    @contextmanager
    def _shared():
        yield session

    monkeypatch.setattr(exec_log, "get_session", _shared)
    yield session
    session.rollback()
    session.close()
    exec_log._current = None


def test_run_writes_stage_rows_with_timing_and_llm(tx):
    run = exec_log.start_run()
    assert run is not None
    with exec_log.stage("factor_screen") as rec:
        rec.summary = "測試段"
        rec.payload = {"top_candidates": [{"stock_id": "2330", "score": 9.9}]}
        rec.add_llm({"prompt_tokens": 42, "completion_tokens": 7})

    row = tx.execute(text("""
        SELECT stage, seq, duration_ms, model_calls, tokens_in, tokens_out,
               summary, payload, status
        FROM execution_log WHERE run_id = :rid
    """), {"rid": run.run_id}).fetchone()
    assert row is not None
    assert row[0] == "factor_screen" and row[1] == 1
    assert row[2] is not None and row[2] >= 0       # 有計時
    assert (row[3], row[4], row[5]) == (1, 42, 7)   # LLM 用量
    assert row[6] == "測試段" and row[8] == "ok"
    payload = row[7] if isinstance(row[7], dict) else json.loads(row[7])
    assert payload["top_candidates"][0]["stock_id"] == "2330"


def test_failed_stage_recorded_and_reraised(tx):
    run = exec_log.start_run()
    with pytest.raises(RuntimeError):
        with exec_log.stage("judge"):
            raise RuntimeError("模型爆了")
    row = tx.execute(text(
        "SELECT status, error_msg FROM execution_log WHERE run_id = :rid"
    ), {"rid": run.run_id}).fetchone()
    assert row[0] == "failed" and "模型爆了" in row[1]


def test_retention_purges_old_rows(tx):
    run = exec_log.start_run()
    # 塞一筆 200 天前的舊資料，再跑一次 start_run 應被清掉
    tx.execute(text("""
        INSERT INTO execution_log (run_id, stage, seq, started_at)
        VALUES (:rid, 'old', 1, now() - interval '200 days')
    """), {"rid": run.run_id})
    exec_log.start_run()
    n = tx.execute(text(
        "SELECT COUNT(*) FROM execution_log WHERE stage = 'old'"
    )).scalar()
    assert n == 0
