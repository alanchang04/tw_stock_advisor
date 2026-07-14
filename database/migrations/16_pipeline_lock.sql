-- Migration 16 — pipeline 互斥鎖（SPEC_REASONING_LAYER P0-1）
--
-- 2026-07-13 排程(GitHub Actions)與本機手動 pipeline 併發執行事故：
-- 候選股法人資料在對方 backfill 途中被讀成全 0（辯論建立在污染輸入上）、
-- daily_recommendations 被兩組推薦交錯寫成 9 列 rank 重複、使用者收到兩則不同推薦。
--
-- 用既有的 pipeline_logs 表當鎖：開跑先 INSERT 一筆 task_name='pipeline_lock'
-- status='running'；此 partial unique index 保證同時只有一個 INSERT 成功。
-- 逾時(>90分)的 running 由程式自動標 failed 後重搶（agent/pipeline_lock.py）。
-- ensure_lock_index() 會冪等自建，此檔僅供紀錄/手動套用。

CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_lock_running
ON pipeline_logs (task_name) WHERE task_name = 'pipeline_lock' AND status = 'running';
