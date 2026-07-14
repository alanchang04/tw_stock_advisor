"""
agent/exec_log.py

決策軌跡（execution log）——規格見 docs/SPEC_PIPELINE_IMPROVEMENTS.md Phase A。

每次 pipeline 執行 = 一個 run（UUID），流程中每個階段寫一列：
耗時、LLM 用量、資料來源+信心分數、人話小總結、完整中間產物(payload)。
同時回答兩個問題：「AI 為什麼選這檔」（Streamlit 決策軌跡頁）與
「20 分鐘花在哪」（逐段 duration_ms）。

設計約束：
  - 寫入失敗絕不讓 pipeline 掛掉（全部 try/except 降級成 log warning）
  - 沒呼叫 start_run() 時（單元測試、ad-hoc 腳本）stage() 是 no-op，不碰 DB
  - payload 上限 100KB/段（防失控；超過截斷並註記）
  - 每次 start_run 自動清掉 >EXEC_LOG_RETENTION_DAYS 的舊紀錄（Neon 空間有限）

用法：
    from agent import exec_log
    exec_log.start_run()                       # pipeline 入口呼叫一次
    with exec_log.stage("factor_screen") as rec:
        ...                                    # 做事
        rec.summary = "全市場 1855 檔 → 初篩 132 檔 → 取前 20"
        rec.payload = {"top20": [...]}
        rec.add_llm(usage)                     # 有 LLM 呼叫才需要
    exec_log.end_run()
"""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from database.connection import get_session

EXEC_LOG_RETENTION_DAYS = 180     # Neon 空間有限：估 ~20KB/run，180 天 ≈ 3.6MB
MAX_PAYLOAD_BYTES = 100_000       # 單段 payload 上限（規格驗收條件）

_current: "ExecRun | None" = None


def ensure_execution_log_table():
    """冪等建表（migration 15+18 同內容，現有 DB 自動補上）。"""
    with get_session() as s:
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS execution_log (
                id          BIGSERIAL   PRIMARY KEY,
                run_id      UUID        NOT NULL,
                stage       VARCHAR(40) NOT NULL,
                seq         SMALLINT    NOT NULL,
                started_at  TIMESTAMPTZ NOT NULL,
                finished_at TIMESTAMPTZ,
                duration_ms INTEGER,
                model_calls SMALLINT    DEFAULT 0,
                tokens_in   INTEGER     DEFAULT 0,
                tokens_out  INTEGER     DEFAULT 0,
                sources     JSONB,
                summary     TEXT,
                payload     JSONB,
                status      VARCHAR(10) DEFAULT 'ok',
                error_msg   TEXT
            )
        """))
        # kind 區分「每日 pipeline」vs「個股分析」等一次性查詢（migration 18）
        s.execute(text("ALTER TABLE execution_log ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'pipeline'"))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_execution_log_run ON execution_log (run_id, seq)"))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_execution_log_time ON execution_log (started_at)"))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_execution_log_kind ON execution_log (kind, started_at)"))


def _truncate_payload(payload) -> str | None:
    """序列化 payload 並強制 100KB 上限（超過截斷並註記，絕不讓單段塞爆表）。"""
    if payload is None:
        return None
    try:
        raw = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"_error": f"payload 無法序列化: {e}"})
    raw_bytes = raw.encode("utf-8")
    if len(raw_bytes) <= MAX_PAYLOAD_BYTES:
        return raw
    # 截斷：按 bytes 切（中文字 3 bytes/字，按字元切會爆量），標記被截斷
    cut = raw_bytes[:80_000].decode("utf-8", errors="ignore")
    return json.dumps({"_truncated": True,
                       "_original_bytes": len(raw_bytes),
                       "head": cut}, ensure_ascii=False)


class StageRec:
    """一個階段的記錄器。caller 設定 summary/payload/sources，LLM 呼叫用 add_llm 累計。"""

    def __init__(self, name: str):
        self.name = name
        self.summary: str | None = None
        self.payload = None
        self.sources = None            # [{"source": "...", "confidence": 0.9}, ...]（Phase B 起有值）
        self.model_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    def add_llm(self, usage=None, calls: int = 1):
        """累計 LLM 用量。usage 吃 litellm 的 response.usage（或同形狀 dict），拿不到就只記次數。"""
        self.model_calls += calls
        if usage is None:
            return
        try:
            get = usage.get if isinstance(usage, dict) else lambda k, d=0: getattr(usage, k, d)
            self.tokens_in += int(get("prompt_tokens", 0) or 0)
            self.tokens_out += int(get("completion_tokens", 0) or 0)
        except Exception:
            pass                       # 用量拿不到就算了，不影響流程


class ExecRun:
    def __init__(self, kind: str = "pipeline"):
        self.run_id = str(uuid.uuid4())
        self.kind = kind
        self.seq = 0

    @contextmanager
    def stage(self, name: str):
        self.seq += 1
        seq = self.seq
        rec = StageRec(name)
        started = datetime.now(timezone.utc)
        status, err = "ok", None
        try:
            yield rec
        except Exception as e:
            status, err = "failed", f"{type(e).__name__}: {str(e)[:400]}"
            raise                                  # 記錄完照樣往外丟，不吞例外
        finally:
            finished = datetime.now(timezone.utc)
            try:
                with get_session() as s:
                    s.execute(text("""
                        INSERT INTO execution_log
                            (run_id, kind, stage, seq, started_at, finished_at, duration_ms,
                             model_calls, tokens_in, tokens_out, sources, summary, payload,
                             status, error_msg)
                        VALUES (:rid, :kind, :st, :seq, :t0, :t1, :ms, :mc, :ti, :to,
                                CAST(:src AS JSONB), :sum, CAST(:pl AS JSONB), :status, :err)
                    """), {
                        "rid": self.run_id, "kind": self.kind, "st": name, "seq": seq,
                        "t0": started, "t1": finished,
                        "ms": int((finished - started).total_seconds() * 1000),
                        "mc": rec.model_calls, "ti": rec.tokens_in, "to": rec.tokens_out,
                        "src": json.dumps(rec.sources, ensure_ascii=False, default=str) if rec.sources else None,
                        "sum": rec.summary,
                        "pl": _truncate_payload(rec.payload),
                        "status": status, "err": err,
                    })
            except Exception as e:                 # 記錄失敗絕不擋 pipeline
                logger.warning(f"execution_log 寫入失敗（不影響流程）: {e}")


def start_run(kind: str = "pipeline") -> ExecRun | None:
    """
    開新 run。建表＋清舊資料＋開新 run。失敗回 None（全程降級為 no-op）。
    kind："pipeline"＝每日排程（預設，決策軌跡頁的 run 列表只顯示這個）；
         "stock_analysis"＝個股隨選分析（可能一天觸發多次，不進 pipeline 列表）。
    """
    global _current
    try:
        ensure_execution_log_table()
        with get_session() as s:
            s.execute(text(
                f"DELETE FROM execution_log WHERE started_at < now() - interval '{EXEC_LOG_RETENTION_DAYS} days'"
            ))
        _current = ExecRun(kind=kind)
        logger.info(f"決策軌跡 run_id={_current.run_id[:8]}…（kind={kind}，保留 {EXEC_LOG_RETENTION_DAYS} 天）")
        return _current
    except Exception as e:
        logger.warning(f"execution_log 初始化失敗（本次不記錄決策軌跡）: {e}")
        _current = None
        return None


def end_run():
    global _current
    _current = None


@contextmanager
def stage(name: str):
    """模組層捷徑：深層模組（llm_advisor 等）不必傳遞 run 物件。
    沒有進行中的 run 時為 no-op（照樣 yield rec，但不寫 DB）。"""
    if _current is None:
        yield StageRec(name)
        return
    with _current.stage(name) as rec:
        yield rec
