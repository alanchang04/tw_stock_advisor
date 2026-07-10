-- Migration 14 — 掛單→隔日開盤成交（階段0b）
--
-- 目的：pipeline 於收盤後 20:00~22:30 才跑，舊版卻用「當日收盤價」直接建倉/平倉，
--       那個價格早已成交完畢、根本買不到（無法實現的成交價假設）。
--       改為兩階段：今晚算訊號 → 寫進 pending_orders → 明晚 pipeline 用當日開盤價成交。
--       這同時也是接券商 API 的架構前置（委託 → 成交回報）。
--
-- 皆為「新增」，不動既有資料。portfolio.ensure_* 也會冪等自建，此檔僅供紀錄/手動套用。

CREATE TABLE IF NOT EXISTS pending_orders (
    id           BIGSERIAL     PRIMARY KEY,
    side         VARCHAR(4)    NOT NULL CHECK (side IN ('buy','sell')),
    stock_id     VARCHAR(10)   NOT NULL REFERENCES stocks(stock_id),
    signal_date  DATE          NOT NULL,      -- 訊號產生日（該日收盤算出）
    signal_price NUMERIC(12,2),               -- 訊號日收盤價（供日後校準滑價：成交價 vs 訊號價）
    reason       TEXT,
    position_id  BIGINT        REFERENCES positions(id),   -- sell 單對應的部位
    status       VARCHAR(10)   NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','filled','cancelled','expired')),
    fill_date    DATE,
    fill_price   NUMERIC(12,2),
    created_at   TIMESTAMPTZ   DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pending_orders_status ON pending_orders (status);

-- 同一檔股票同一方向同時只能有一張未成交委託
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_orders_open
    ON pending_orders (stock_id, side) WHERE status = 'pending';

-- positions 新增欄位：不改寫 return_pct 既有語意（舊 39 筆已平倉仍是舊模型的毛報酬），
-- 而是另存訊號價與淨報酬，方便前後對照與量測成本拖累。
ALTER TABLE positions ADD COLUMN IF NOT EXISTS signal_price      NUMERIC(12,2);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS exit_signal_price NUMERIC(12,2);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS net_return_pct    NUMERIC(8,4);
