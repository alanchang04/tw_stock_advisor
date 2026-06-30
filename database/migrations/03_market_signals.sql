-- ============================================================
--  Migration 03 — ETF追蹤 + 市場情報
--  執行：psql $DATABASE_URL -f database/migrations/03_market_signals.sql
-- ============================================================

-- ETF 追蹤名單
CREATE TABLE IF NOT EXISTS etf_watchlist (
    etf_id      VARCHAR(10)  PRIMARY KEY,
    etf_name    VARCHAR(100) NOT NULL,
    etf_type    VARCHAR(20)  NOT NULL   -- 'active' / 'passive' / 'high_dividend'
                    CHECK (etf_type IN ('active','passive','high_dividend')),
    is_active   BOOLEAN      DEFAULT TRUE,
    note        TEXT,
    updated_at  TIMESTAMPTZ  DEFAULT NOW()
);

-- 預設追蹤的 ETF
INSERT INTO etf_watchlist (etf_id, etf_name, etf_type, note) VALUES
    -- 主動型（任意日換股，優先追蹤）
    ('00981A', '統一台股增長',       'active',        '主動型，任意日換股'),
    ('00999A', '野村臺灣高息',       'active',        '主動型，任意日換股'),
    ('00991A', '復華未來50',         'active',        '主動型，任意日換股'),
    ('00982A', '群益台灣強棒',       'active',        '主動型，任意日換股'),
    ('00992A', '群益科技創新',       'active',        '主動型，任意日換股'),
    ('00400A', '國泰動能高息',       'active',        '主動型，任意日換股'),
    -- 被動指數型
    ('0050',   '元大台灣50',         'passive',       '大型指數參考'),
    ('006208', '富邦台50',           'passive',       '大型指數參考'),
    ('009816', '凱基台灣TOP50',      'passive',       '大型指數參考'),
    ('00891',  '中信關鍵半導體',     'passive',       '半導體主題'),
    -- 高股息型（季度換股）
    ('0056',   '元大高股息',         'high_dividend', '季度換股'),
    ('00878',  '國泰永續高息',       'high_dividend', '季度換股'),
    ('00919',  '群益精選高息',       'high_dividend', '季度換股'),
    ('00929',  '復華台科優息',       'high_dividend', '季度換股'),
    ('00940',  '元大台灣價值高息',   'high_dividend', '季度換股')
ON CONFLICT (etf_id) DO NOTHING;

-- ETF 持股快照（每次抓到即存）
CREATE TABLE IF NOT EXISTS etf_holdings (
    etf_id        VARCHAR(10)  NOT NULL,
    stock_id      VARCHAR(10)  NOT NULL,
    stock_name    VARCHAR(100),
    weight_pct    NUMERIC(8,4),          -- 持股比例 %
    shares        BIGINT,                -- 持股張數（如有）
    snapshot_date DATE         NOT NULL,
    PRIMARY KEY (etf_id, stock_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_etf_holdings_date
    ON etf_holdings (etf_id, snapshot_date DESC);

-- ETF 換股記錄
CREATE TABLE IF NOT EXISTS etf_changes (
    id            BIGSERIAL    PRIMARY KEY,
    etf_id        VARCHAR(10)  NOT NULL,
    etf_name      VARCHAR(100),
    stock_id      VARCHAR(10)  NOT NULL,
    stock_name    VARCHAR(100),
    change_type   VARCHAR(20)  NOT NULL
                      CHECK (change_type IN ('added','removed','increased','decreased')),
    old_weight    NUMERIC(8,4),
    new_weight    NUMERIC(8,4),
    detected_date DATE         NOT NULL DEFAULT CURRENT_DATE,
    created_at    TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_etf_changes_date
    ON etf_changes (detected_date DESC);
CREATE INDEX IF NOT EXISTS idx_etf_changes_stock
    ON etf_changes (stock_id, detected_date DESC);

-- 市場情報（新聞 / YouTube 摘要 / 主動觸發訊號）
CREATE TABLE IF NOT EXISTS market_signals (
    id              BIGSERIAL    PRIMARY KEY,
    signal_type     VARCHAR(20)  NOT NULL
                        CHECK (signal_type IN ('etf_change','news','youtube','mops')),
    source          VARCHAR(100),          -- 來源（e.g. '鉅亨網', '錢線百分百'）
    title           TEXT         NOT NULL,
    summary         TEXT,                  -- Gemini 摘要
    url             TEXT,
    related_stocks  TEXT[],                -- 提及的股票代號陣列
    sentiment       VARCHAR(10)
                        CHECK (sentiment IN ('positive','negative','neutral')),
    signal_date     DATE         NOT NULL DEFAULT CURRENT_DATE,
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_market_signals_date
    ON market_signals (signal_date DESC);
CREATE INDEX IF NOT EXISTS idx_market_signals_type
    ON market_signals (signal_type, signal_date DESC);
