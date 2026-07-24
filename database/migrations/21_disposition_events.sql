-- Migration 21 — 處置股 / 注意股事件（SPEC_QUANT_UPGRADE.md §2.4，P0 資料地基最後一項）
--
-- 為什麼需要：SPEC_STRATEGY_MIDCAP §2.1 硬門檻表的「排除警示」一直掛著「(待補)」，
-- 程式碼中完全沒有處置股過濾邏輯。後果有兩個，而且方向相反：
--   1. 回測「太樂觀」：處置期間是人工管制撮合（約每五分鐘一次）+ 委託達十交易單位
--      須預收款券。回測假設隔日開盤照常成交、只扣 30bp 滑價，嚴重低估真實交易成本。
--   2. 即時選股「會真的推薦到」：處置多半由爆量急漲觸發，那正是動能策略最愛的形態，
--      成交金額門檻擋不住。
--
-- 來源（官方，免 key，實測可回溯至 2015）：
--   處置 https://www.twse.com.tw/rwd/zh/announcement/punish?startDate=&endDate=
--   注意 https://www.twse.com.tw/rwd/zh/announcement/notice?startDate=&endDate=
--
-- 已知缺口（誠實記錄）：
--   - 只有上市（TWSE）。上櫃 www.tpex.org.tw 憑證缺 Subject Key Identifier，
--     Python 3.14 嚴格驗證會拒絕（同 margin_fetcher 的限制）→ 上櫃處置股目前擋不掉。
--   - 只存 4 位數個股代號，權證/ETN（6 位數，佔原始資料六成以上）不在候選池故不收。
--
-- notice_events 目前「只存不用」：§2.4 只要求排除處置股。注意股一年 3000+ 筆、涵蓋
-- 相當大比例的活躍股，直接全排會過度殺傷；要當因子用必須先過 P1 的 IC 檢驗
-- （見 §5.5 券資比的教訓：IC 過關不代表能加進策略）。
--
-- data_pipeline/fetchers/disposition_fetcher.py 的 ensure_disposition_tables() 會
-- 冪等自建，此檔僅供紀錄。

CREATE TABLE IF NOT EXISTS disposition_events (
    id            BIGSERIAL   PRIMARY KEY,
    stock_id      VARCHAR(10) NOT NULL,
    announce_date DATE,                       -- 公布日（可能與處置起日不同）
    start_date    DATE        NOT NULL,       -- 處置起日（含）
    end_date      DATE        NOT NULL,       -- 處置迄日（含）
    cumulative    INTEGER,                    -- 累計處置次數
    reason        VARCHAR(100),               -- 處置條件，如「連續三次」
    measure       VARCHAR(50),                -- 第一次處置／第二次處置
    market        VARCHAR(10) NOT NULL DEFAULT 'TWSE',
    created_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE (stock_id, start_date)
);

-- 沒有 FK 到 stocks：處置清單含已下市個股，加 FK 會讓歷史回補掉資料（＝倖存者偏誤）
CREATE INDEX IF NOT EXISTS idx_disposition_stock  ON disposition_events (stock_id, start_date, end_date);
CREATE INDEX IF NOT EXISTS idx_disposition_period ON disposition_events (start_date, end_date);


CREATE TABLE IF NOT EXISTS notice_events (
    id          BIGSERIAL   PRIMARY KEY,
    stock_id    VARCHAR(10) NOT NULL,
    notice_date DATE        NOT NULL,
    reason      VARCHAR(200),
    market      VARCHAR(10) NOT NULL DEFAULT 'TWSE',
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (stock_id, notice_date, reason)
);

CREATE INDEX IF NOT EXISTS idx_notice_stock ON notice_events (stock_id, notice_date);
