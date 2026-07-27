-- Migration 24 — 選股日誌 / 前向計分板（2026-07-26，方向 B）
--
-- 為什麼：回測會被過擬合污染，唯一乾淨的驗證是「向前記錄真實決定」。記使用者從每日
-- 排行（或自己判斷）挑的股票 + 理由，持續對照三條線：你的挑選 vs 前20整體 vs 0050。
-- 若你的挑選贏過「無腦買整份前20」→ 你的判斷（供應鏈/大戶/讀報告）真的加分。
--
-- 「前20整體」對照組不必另存——已存在 ranked_watchlist_history（每晚 pipeline 寫入）。
-- 報酬一律 close-to-close（挑選日收盤→現在收盤），三條線同慣例才公平（判斷歸因，
-- 非實際成交損益）。多使用者：每筆帶 user_id。
--
-- agent/pick_journal.py 的 ensure_pick_journal_table() 會冪等自建，此檔僅供紀錄。

CREATE TABLE IF NOT EXISTS pick_journal (
    id              BIGSERIAL   PRIMARY KEY,
    user_id         INTEGER     NOT NULL,
    pick_date       DATE        NOT NULL,          -- 決策依據的交易日
    stock_id        VARCHAR(10) NOT NULL,
    stock_name      VARCHAR(50),
    entry_ref_price NUMERIC(12,4) NOT NULL,        -- 挑選日收盤（參考基準，非成交價）
    rank_at_pick    INTEGER,                       -- 當日在前20的排名（NULL＝榜外，純判斷）
    note            VARCHAR(200),                  -- 理由／用到的邏輯
    status          VARCHAR(10) NOT NULL DEFAULT 'open',
    exit_date       DATE,
    exit_price      NUMERIC(12,4),
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pick_journal_user ON pick_journal (user_id, status);
