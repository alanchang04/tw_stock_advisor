-- Migration 23 — TDCC 集保股權分散：大戶持股集中度（2026-07-26，SPEC_QUANT_UPGRADE §2.6）
--
-- 為什麼：§2.6 標記「集保股權分散（TDCC 週更、免費）是中小型股『聰明錢』偵測的正宗
-- 資料」。使用者獨立想到同一件事（觀察大戶是否默默進場）。
--
-- ⚠️ 這不是驗證過的因子，是決策支援訊號。免費歷史只有 ~1 年（OpenData 只給當週、
-- smWeb 只到 51 週；10 年要 FinMind 付費），1 年＝單一 régime＝無法做嚴謹回測。
-- 策略：向前累積（每週存一次，慢慢建乾淨歷史）+ 當前值當未驗證訊號給人看。
--
-- 大戶集中度＝級15（1,000,001股以上＝千張大戶）占比%。
-- data_pipeline/fetchers/tdcc_fetcher.py 的 ensure_tdcc_table() 會冪等自建，此檔僅供紀錄。

CREATE TABLE IF NOT EXISTS tdcc_holdings (
    data_date        DATE        NOT NULL,       -- 資料週（西元）
    stock_id         VARCHAR(10) NOT NULL,       -- 只收 4 位數個股
    big_holder_pct   NUMERIC(6,2),               -- 級15 千張大戶 占集保比例%
    big_holder_count INTEGER,                    -- 級15 人數（大戶戶數）
    gt400_pct        NUMERIC(6,2),               -- 級12~15（400張以上）占比合計%
    total_holders    INTEGER,                    -- 總股東人數（級1~15 人數合計）
    created_at       TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (data_date, stock_id)            -- 同週重跑覆蓋（冪等）
);

CREATE INDEX IF NOT EXISTS idx_tdcc_stock ON tdcc_holdings (stock_id, data_date);
