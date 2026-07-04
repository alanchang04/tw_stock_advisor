-- Migration 10 — 月營收（財報因子，來源 MOPS 彙總頁）
-- 執行：Neon SQL Editor 貼上執行一次

CREATE TABLE IF NOT EXISTS monthly_revenue (
    stock_id   VARCHAR(10) NOT NULL,
    year_month VARCHAR(7)  NOT NULL,     -- '2026-05'
    revenue    BIGINT,                   -- 當月營收（千元）
    mom_pct    NUMERIC(12,2),            -- 上月比較增減 %
    yoy_pct    NUMERIC(12,2),            -- 去年同月增減 %
    PRIMARY KEY (stock_id, year_month)
);

CREATE INDEX IF NOT EXISTS idx_monthly_revenue_ym
    ON monthly_revenue (year_month DESC);
