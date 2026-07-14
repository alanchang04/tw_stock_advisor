-- Migration 18 — execution_log 加 kind 欄位，區分「每日 pipeline」vs「個股分析」
--
-- 個股分析（agent/stock_analysis.py，使用者輸入單一股票的隨選分析）沿用決策軌跡
-- 的架構（同一張表、同樣的 stage/summary/payload 概念），但不該混進決策軌跡頁
-- 的 pipeline run 列表（stage 命名體系不同、且可能一天觸發多次）。用 kind 區分，
-- 預設 'pipeline' 保持舊資料/舊呼叫相容。
--
-- agent/exec_log.py 的 ensure_execution_log_table() 會冪等自建，此檔僅供紀錄。

ALTER TABLE execution_log ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'pipeline';
CREATE INDEX IF NOT EXISTS idx_execution_log_kind ON execution_log (kind, started_at);
