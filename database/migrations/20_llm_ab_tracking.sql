-- Migration 20 — LLM 平行 A/B 量測（SPEC_QUANT_UPGRADE.md 決策點3，2026-07-20補做）
-- 執行：Neon SQL Editor 貼上執行一次（agent/llm_ab_tracking.py 也會冪等自動補建）
--
-- 每天記錄「量化引擎自己會選誰」(source='quant_only') 跟「LLM辯論+裁決最後選誰」
-- (source='llm')，同一個候選池、同一天，事後拿實際後續報酬回頭比對兩邊誰選得好。
-- 不需要多打任何一次LLM API——這兩組資料本來就是每日pipeline已經算出來的東西，
-- 只是之前沒有額外存一份方便比對的紀錄。

CREATE TABLE IF NOT EXISTS llm_ab_tracking (
    signal_date DATE         NOT NULL,
    source      VARCHAR(20)  NOT NULL,   -- 'quant_only' | 'llm'
    stock_id    VARCHAR(10)  NOT NULL,
    rank        SMALLINT,                -- 這組裡的名次（1=最高）
    score       NUMERIC,                 -- quant_only 存評分公式分數；llm 空
    reason      TEXT,                    -- llm 存裁決理由；quant_only 空
    PRIMARY KEY (signal_date, source, stock_id)
);

CREATE INDEX IF NOT EXISTS idx_llm_ab_tracking_date ON llm_ab_tracking (signal_date);
