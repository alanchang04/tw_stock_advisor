-- Migration 05 — 新增 smart_money 訊號類型
-- 執行：Neon SQL Editor 貼上，一次執行即可

ALTER TABLE market_signals
    DROP CONSTRAINT IF EXISTS market_signals_signal_type_check;

ALTER TABLE market_signals
    ADD CONSTRAINT market_signals_signal_type_check
    CHECK (signal_type IN ('etf_change','news','youtube','mops','digest','smart_money'));
