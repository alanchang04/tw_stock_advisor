-- Migration 08 — 使用者自建追蹤清單
-- 執行：Neon SQL Editor 貼上執行一次

CREATE TABLE IF NOT EXISTS user_watchlist (
    stock_id     VARCHAR(10) PRIMARY KEY REFERENCES stocks(stock_id),
    added_date   DATE NOT NULL DEFAULT CURRENT_DATE,
    note         TEXT,
    target_price NUMERIC(12,2),          -- 選填：理想買入價
    last_signal  TEXT,                   -- 最近一次買點判斷
    signal_date  DATE
);
