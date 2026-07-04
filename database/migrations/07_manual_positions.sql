-- Migration 07 — positions 支援手動建倉（我的持倉功能）
-- 執行：Neon SQL Editor 貼上執行一次

ALTER TABLE positions ADD COLUMN IF NOT EXISTS source VARCHAR(10) NOT NULL DEFAULT 'ai';
ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_source_check;
ALTER TABLE positions ADD CONSTRAINT positions_source_check CHECK (source IN ('ai','manual'));

ALTER TABLE positions ADD COLUMN IF NOT EXISTS shares INTEGER;      -- 股數（手動倉用）
ALTER TABLE positions ADD COLUMN IF NOT EXISTS last_advice TEXT;    -- 最近一次 AI 建議
ALTER TABLE positions ADD COLUMN IF NOT EXISTS advice_date DATE;    -- 建議日期

-- 手動倉可能同股票分批買，唯一約束只保留給 AI 部位
ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_stock_id_entry_date_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_positions_ai
    ON positions (stock_id, entry_date) WHERE source = 'ai';
