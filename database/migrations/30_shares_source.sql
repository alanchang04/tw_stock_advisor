-- 30_shares_source.sql — 區分「實際成交的股數」與「事後依規則推算的股數」
--
-- 為什麼需要這個欄位
-- ------------------
-- app.py 原本明文寫著「系統不會用目前參數倒推舊股數」，那是刻意的：
-- 推算值與成交值長得一模一樣時，沒有人分得出哪個是紀錄、哪個是模型輸出。
--
-- 2026-08-18 使用者決定為 10 檔 AI 紙上部位補算金額（否則損益$欄與首頁、
-- 歷史績效兩個指標永遠是空的）。補算本身沒問題，但**必須看得出來是補算的**，
-- 所以反轉該政策的同時加上這個標記，而不是靜默填值。
--
-- recorded      實際成交回報的股數（例如使用者自己的手動持倉）
-- reconstructed 事後依 STRATEGY 的部位大小規則推算（entry_share_count）
-- NULL          舊資料，來源不明——不得當成上述任一種

ALTER TABLE positions ADD COLUMN IF NOT EXISTS shares_source VARCHAR(20);

ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_shares_source_valid;
ALTER TABLE positions ADD CONSTRAINT positions_shares_source_valid
    CHECK (shares_source IS NULL OR shares_source IN ('recorded', 'reconstructed'));

COMMENT ON COLUMN positions.shares_source IS
    'recorded=實際成交；reconstructed=依規則事後推算；NULL=來源不明';
