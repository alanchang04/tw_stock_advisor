-- Migration 17 — 資料品質驗證器紀錄（規格 SPEC_PIPELINE_IMPROVEMENTS.md Phase B）
--
-- 規則驗證器（agent/quality_gate.py）每次觸發寫一列：來源、欄位、期望vs實際、嚴重度。
-- 這是未來「學習來源權重」的訓練資料——現在只累積，不學習（規格明確暫緩：
-- 觸發條件＝累積≥300筆含人工裁決的紀錄後才重開此議題）。
-- 同時是「來源記分卡」的統計來源：近30日觸發次數/錯誤率 → 信心分數，餵進
-- execution_log.sources 顯示於決策軌跡頁。
--
-- agent/quality_gate.py 的 ensure_discrepancy_log_table() 會冪等自建，此檔僅供紀錄。

CREATE TABLE IF NOT EXISTS discrepancy_log (
    id           BIGSERIAL    PRIMARY KEY,
    detected_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    check_name   VARCHAR(40)  NOT NULL,
    source       VARCHAR(40)  NOT NULL,
    stock_id     VARCHAR(10),
    field        VARCHAR(40),
    expected     TEXT,
    actual       TEXT,
    severity     VARCHAR(10)  NOT NULL DEFAULT 'warn' CHECK (severity IN ('warn', 'error')),
    note         TEXT,
    resolution   VARCHAR(10)  CHECK (resolution IN ('confirmed_bad', 'false_positive', NULL))
);

CREATE INDEX IF NOT EXISTS idx_discrepancy_log_time   ON discrepancy_log (detected_at);
CREATE INDEX IF NOT EXISTS idx_discrepancy_log_source ON discrepancy_log (source, detected_at);
