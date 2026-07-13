-- Migration 15 — 決策軌跡 execution_log（規格：docs/SPEC_PIPELINE_IMPROVEMENTS.md Phase A）
--
-- 每次 pipeline 執行(run_id)的每個階段一列：耗時、LLM 用量、資料來源+信心分數、
-- 人話小總結、完整中間產物(payload JSONB，單段上限 100KB 由程式端強制)。
-- 保留 180 天，每次 pipeline 開始時自動 DELETE 過期列（程式端 exec_log.start_run）。
-- agent/exec_log.py 的 ensure_execution_log_table() 會冪等自建，此檔僅供紀錄/手動套用。

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
);

CREATE INDEX IF NOT EXISTS idx_execution_log_run  ON execution_log (run_id, seq);
CREATE INDEX IF NOT EXISTS idx_execution_log_time ON execution_log (started_at);
