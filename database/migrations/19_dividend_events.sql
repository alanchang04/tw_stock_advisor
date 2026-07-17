-- Migration 19 — 除權息事件 + 下市股票清單（SPEC_QUANT_UPGRADE.md P0-2）
--
-- 個股除權息還原引擎的資料來源：沒有這張表，回測用未還原價，除息日的假跳空
-- 會誤觸出場規則（跌破實體底/前波低點/停損），股息也完全沒算進報酬。
-- 來源：TWSE https://www.twse.com.tw/exchangeReport/TWT49U（startDate/endDate）、
--       TPEX  https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ（startDate/endDate）。
--
-- 下市股票清單：沒有這張表，回測用的候選池永遠是「今天還活著的股票」，過去
-- 曾經飆漲又下市的地雷股不在樣本裡，10年窗的報酬會系統性高估（倖存者偏誤）。
-- 來源：TWSE openapi https://openapi.twse.com.tw/v1/company/suspendListingCsvAndHtml
-- （TPEX 尚未找到對應清單端點，暫缺，見 SPEC_QUANT_UPGRADE.md 誠實揭露）。
--
-- data_pipeline/fetchers/dividend_fetcher.py 的 ensure_dividend_events_table() 會
-- 冪等自建，此檔僅供紀錄。

CREATE TABLE IF NOT EXISTS dividend_events (
    id              BIGSERIAL    PRIMARY KEY,
    stock_id        VARCHAR(10)  NOT NULL REFERENCES stocks(stock_id),
    ex_date         DATE         NOT NULL,           -- 除權息日
    pre_close       NUMERIC(12,4),                   -- 除權息前一日收盤價
    ref_price       NUMERIC(12,4),                    -- 除權息參考價（官方公告）
    cash_dividend    NUMERIC(12,4) DEFAULT 0,          -- 息值（現金股利部分）
    stock_dividend_ratio NUMERIC(12,6) DEFAULT 0,      -- 權值（股票股利/減資等造成的比例調整）
    event_type      VARCHAR(10)  NOT NULL DEFAULT '除息'
                        CHECK (event_type IN ('除息', '除權', '除權息')),
    market          VARCHAR(10)  NOT NULL CHECK (market IN ('TWSE', 'TPEX')),
    created_at      TIMESTAMPTZ  DEFAULT now(),
    UNIQUE (stock_id, ex_date)
);

CREATE INDEX IF NOT EXISTS idx_dividend_events_stock ON dividend_events (stock_id, ex_date);
CREATE INDEX IF NOT EXISTS idx_dividend_events_date   ON dividend_events (ex_date);


CREATE TABLE IF NOT EXISTS delisted_stocks (
    stock_id        VARCHAR(10)  PRIMARY KEY,
    stock_name      VARCHAR(50),
    delisting_date  DATE,
    market          VARCHAR(10)  CHECK (market IN ('TWSE', 'TPEX')),
    created_at      TIMESTAMPTZ  DEFAULT now()
);
