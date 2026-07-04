-- Migration 11 — market_signals 防重複（DB 層保證，pipeline 重跑不再靠應用層防重）
-- 執行：Neon SQL Editor 貼上執行一次

-- 先清掉既有重複（保留 id 最小者）
DELETE FROM market_signals a USING market_signals b
WHERE a.id > b.id
  AND a.signal_type = b.signal_type
  AND a.title = b.title
  AND a.signal_date = b.signal_date;

CREATE UNIQUE INDEX IF NOT EXISTS uq_market_signals_type_title_date
    ON market_signals (signal_type, title, signal_date);
