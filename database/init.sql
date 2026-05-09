-- ============================================================
--  Taiwan Stock Advisor — Database Schema
--  PostgreSQL 16
-- ============================================================

-- 啟用 uuid 擴充（唯一 id 用）
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- 1. 股票基本資料
-- ============================================================
CREATE TABLE IF NOT EXISTS stocks (
    stock_id        VARCHAR(20)  PRIMARY KEY,          -- 股票代號，例如 "2330"
    stock_name      VARCHAR(100) NOT NULL,              -- 台積電
    market          VARCHAR(10)  NOT NULL               -- TWSE / TPEX
                        CHECK (market IN ('TWSE','TPEX')),
    industry_code   VARCHAR(20),                        -- 產業代碼（對應 industries.code）
    listing_date    DATE,
    is_active       BOOLEAN      DEFAULT TRUE,
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  DEFAULT NOW()
);

COMMENT ON TABLE stocks IS '上市上櫃股票基本資料';

-- ============================================================
-- 2. 產業 / 族群分類（來源：MoneyDJ 爬蟲）
-- ============================================================
CREATE TABLE IF NOT EXISTS industries (
    code            VARCHAR(20)  PRIMARY KEY,           -- e.g. "semiconductor"
    name_zh         VARCHAR(100) NOT NULL,              -- 半導體
    name_en         VARCHAR(100),
    parent_code     VARCHAR(20)  REFERENCES industries(code),
    source          VARCHAR(50)  DEFAULT 'moneydj',
    updated_at      TIMESTAMPTZ  DEFAULT NOW()
);

-- 股票 ↔ 族群（多對多，一支股票可屬多個概念股族群）
CREATE TABLE IF NOT EXISTS stock_industry_map (
    stock_id        VARCHAR(10)  REFERENCES stocks(stock_id) ON DELETE CASCADE,
    industry_code   VARCHAR(20)  REFERENCES industries(code) ON DELETE CASCADE,
    PRIMARY KEY (stock_id, industry_code)
);

-- ============================================================
-- 3. 每日股價（OHLCV + 基本指標）
-- ============================================================
CREATE TABLE IF NOT EXISTS daily_prices (
    id              BIGSERIAL    PRIMARY KEY,
    stock_id        VARCHAR(10)  NOT NULL REFERENCES stocks(stock_id),
    trade_date      DATE         NOT NULL,
    open            NUMERIC(12,2),
    high            NUMERIC(12,2),
    low             NUMERIC(12,2),
    close           NUMERIC(12,2),
    volume          BIGINT,                             -- 成交股數
    turnover        BIGINT,                             -- 成交金額
    change_pct      NUMERIC(8,4),                       -- 漲跌幅 %
    UNIQUE (stock_id, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_daily_prices_stock_date
    ON daily_prices (stock_id, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_daily_prices_date
    ON daily_prices (trade_date DESC);

-- ============================================================
-- 4. 三大法人籌碼
-- ============================================================
CREATE TABLE IF NOT EXISTS institutional_trading (
    id              BIGSERIAL    PRIMARY KEY,
    stock_id        VARCHAR(10)  NOT NULL REFERENCES stocks(stock_id),
    trade_date      DATE         NOT NULL,
    foreign_buy     BIGINT       DEFAULT 0,             -- 外資買超（張）
    foreign_sell    BIGINT       DEFAULT 0,
    foreign_net     BIGINT       DEFAULT 0,             -- 外資淨買超
    invest_buy      BIGINT       DEFAULT 0,             -- 投信買超
    invest_sell     BIGINT       DEFAULT 0,
    invest_net      BIGINT       DEFAULT 0,
    dealer_buy      BIGINT       DEFAULT 0,             -- 自營商買超
    dealer_sell     BIGINT       DEFAULT 0,
    dealer_net      BIGINT       DEFAULT 0,
    total_net       BIGINT       DEFAULT 0,             -- 三大法人合計淨買超
    UNIQUE (stock_id, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_inst_stock_date
    ON institutional_trading (stock_id, trade_date DESC);

-- ============================================================
-- 5. 融資融券
-- ============================================================
CREATE TABLE IF NOT EXISTS margin_trading (
    id              BIGSERIAL    PRIMARY KEY,
    stock_id        VARCHAR(10)  NOT NULL REFERENCES stocks(stock_id),
    trade_date      DATE         NOT NULL,
    margin_balance  BIGINT,                             -- 融資餘額（張）
    margin_change   BIGINT,                             -- 融資變化
    short_balance   BIGINT,                             -- 融券餘額（張）
    short_change    BIGINT,
    UNIQUE (stock_id, trade_date)
);

-- ============================================================
-- 6. 財務報表（季報）
-- ============================================================
CREATE TABLE IF NOT EXISTS financials (
    id              BIGSERIAL    PRIMARY KEY,
    stock_id        VARCHAR(10)  NOT NULL REFERENCES stocks(stock_id),
    year            SMALLINT     NOT NULL,
    quarter         SMALLINT     NOT NULL CHECK (quarter BETWEEN 1 AND 4),
    revenue         BIGINT,                             -- 營收（千元）
    gross_profit    BIGINT,
    operating_income BIGINT,
    net_income      BIGINT,
    eps             NUMERIC(10,2),                      -- 每股盈餘
    roe             NUMERIC(8,4),                       -- 股東權益報酬率 %
    roa             NUMERIC(8,4),
    gross_margin    NUMERIC(8,4),                       -- 毛利率 %
    operating_margin NUMERIC(8,4),
    debt_ratio      NUMERIC(8,4),
    UNIQUE (stock_id, year, quarter)
);

-- ============================================================
-- 7. 技術指標（計算後快取，避免每次重算）
-- ============================================================
CREATE TABLE IF NOT EXISTS technical_indicators (
    id              BIGSERIAL    PRIMARY KEY,
    stock_id        VARCHAR(10)  NOT NULL REFERENCES stocks(stock_id),
    trade_date      DATE         NOT NULL,
    ma5             NUMERIC(12,2),
    ma10            NUMERIC(12,2),
    ma20            NUMERIC(12,2),
    ma60            NUMERIC(12,2),
    ma120           NUMERIC(12,2),
    ma240           NUMERIC(12,2),
    rsi14           NUMERIC(8,4),
    macd            NUMERIC(12,4),
    macd_signal     NUMERIC(12,4),
    macd_hist       NUMERIC(12,4),
    bb_upper        NUMERIC(12,2),                      -- 布林通道上軌
    bb_middle       NUMERIC(12,2),
    bb_lower        NUMERIC(12,2),
    -- 訊號旗標（分析層計算後寫入）
    signal_ma_cross  SMALLINT    DEFAULT 0,             -- 1=黃金交叉 -1=死亡交叉
    signal_breakout  SMALLINT    DEFAULT 0,             -- 1=突破支撐/壓力
    UNIQUE (stock_id, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_tech_stock_date
    ON technical_indicators (stock_id, trade_date DESC);

-- ============================================================
-- 8. 族群輪動熱度（分析層每日計算）
-- ============================================================
CREATE TABLE IF NOT EXISTS sector_momentum (
    id              BIGSERIAL    PRIMARY KEY,
    industry_code   VARCHAR(20)  NOT NULL REFERENCES industries(code),
    calc_date       DATE         NOT NULL,
    avg_change_pct  NUMERIC(8,4),                       -- 族群平均漲幅
    rising_count    SMALLINT,                           -- 上漲股票數
    total_count     SMALLINT,                           -- 族群總股票數
    momentum_score  NUMERIC(8,4),                       -- 綜合熱度分數
    UNIQUE (industry_code, calc_date)
);

-- ============================================================
-- 9. 重大訊息公告（MOPS RSS）
-- ============================================================
CREATE TABLE IF NOT EXISTS announcements (
    id              BIGSERIAL    PRIMARY KEY,
    stock_id        VARCHAR(10)  REFERENCES stocks(stock_id),
    announced_at    TIMESTAMPTZ  NOT NULL,
    title           TEXT         NOT NULL,
    content         TEXT,
    category        VARCHAR(50),                        -- 利多 / 利空 / 中性（LLM分類）
    source_url      TEXT,
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

-- ============================================================
-- 10. YouTube 情報快取
-- ============================================================
CREATE TABLE IF NOT EXISTS yt_insights (
    id              BIGSERIAL    PRIMARY KEY,
    video_id        VARCHAR(20)  NOT NULL UNIQUE,
    channel_name    VARCHAR(100),
    title           TEXT,
    published_at    TIMESTAMPTZ,
    transcript_raw  TEXT,                               -- 原始字幕
    summary         TEXT,                               -- LLM 摘要
    mentioned_stocks JSONB,                             -- {"2330": "看多", "2454": "觀察"}
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

-- ============================================================
-- 11. 每日選股推薦結果（LLM Agent 輸出）
-- ============================================================
CREATE TABLE IF NOT EXISTS daily_recommendations (
    id              BIGSERIAL    PRIMARY KEY,
    rec_date        DATE         NOT NULL,
    stock_id        VARCHAR(10)  NOT NULL REFERENCES stocks(stock_id),
    rank            SMALLINT     NOT NULL CHECK (rank BETWEEN 1 AND 10),
    score           NUMERIC(8,4),                       -- 綜合評分
    reason          TEXT,                               -- LLM 產生的推薦理由
    tech_signals    JSONB,                              -- 技術訊號快照
    fund_signals    JSONB,                              -- 籌碼面快照
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE (rec_date, stock_id)
);

-- ============================================================
-- 12. 系統日誌（pipeline 執行紀錄）
-- ============================================================
CREATE TABLE IF NOT EXISTS pipeline_logs (
    id              BIGSERIAL    PRIMARY KEY,
    task_name       VARCHAR(100) NOT NULL,
    status          VARCHAR(20)  NOT NULL
                        CHECK (status IN ('running','success','failed')),
    started_at      TIMESTAMPTZ  DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    rows_affected   INTEGER,
    error_msg       TEXT
);

-- ============================================================
-- Helper：自動更新 updated_at 欄位
-- ============================================================
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_stocks_updated
    BEFORE UPDATE ON stocks
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();