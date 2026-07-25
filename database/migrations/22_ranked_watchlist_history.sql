-- Migration 22 — 每日 AI 因子排名歷史（2026-07-25，聰明日級通知「方向 A」）
--
-- 為什麼需要：每日排名的「進榜/退榜」異動通知，得把每天的「AI 因子排名前 N」存下來
-- 才能跟昨天 diff。edge 是日級的（營收月更、投信收盤後才公布），盤中沒有已驗證訊號
-- 可反應，所以正確的通知不是「更即時」，是「收盤後算完、只在該理你時叫你」。
--
-- agent/watchlist_alerts.py 的 ensure_ranking_table() 會冪等自建，此檔僅供紀錄。

CREATE TABLE IF NOT EXISTS ranked_watchlist_history (
    rec_date      DATE        NOT NULL,       -- 交易日
    stock_id      VARCHAR(10) NOT NULL,
    rank          INTEGER     NOT NULL,       -- 當日排名（1 = 最高分）
    score         NUMERIC(10,4),              -- AI 綜合分數
    invest_streak INTEGER,                    -- 投信連買天數（已驗證因子，供進榜加星）
    rev_yoy       NUMERIC(10,2),              -- 月營收年增（已驗證因子）
    created_at    TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (rec_date, stock_id)          -- 同日重跑覆蓋（冪等）
);

CREATE INDEX IF NOT EXISTS idx_ranked_wl_date ON ranked_watchlist_history (rec_date);
