-- Migration 13 — 統一主動式 ETF 每日追蹤
-- 1) 新增 00403A 主動統一升級50 到追蹤名單
-- 2) 清除 00981A/00403A 的舊 MoneyDJ 月更快照（改用官網每日全持股，
--    避免首次比對到過期的前十大資料產生大量假換股訊號）

INSERT INTO etf_watchlist (etf_id, etf_name, etf_type, note) VALUES
    ('00403A', '主動統一升級50', 'active', '統一官網每日全持股追蹤')
ON CONFLICT (etf_id) DO NOTHING;

UPDATE etf_watchlist
SET note = '統一官網每日全持股追蹤'
WHERE etf_id = '00981A';

DELETE FROM etf_holdings WHERE etf_id IN ('00981A', '00403A');
DELETE FROM etf_changes  WHERE etf_id IN ('00981A', '00403A');
