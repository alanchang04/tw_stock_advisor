-- Migration 04 — 新增 digest 類型到 market_signals
-- 執行：Neon SQL Editor 貼上執行一次

ALTER TABLE market_signals
    DROP CONSTRAINT IF EXISTS market_signals_signal_type_check;

ALTER TABLE market_signals
    ADD CONSTRAINT market_signals_signal_type_check
    CHECK (signal_type IN ('etf_change','news','youtube','mops','digest'));
