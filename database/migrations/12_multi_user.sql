-- Migration 12 — 多使用者：帳號 / 多組命名自選清單 / 持倉歸戶
-- 執行：由 scripts/migrate_multi_user.py 執行（含資料搬移與密碼雜湊，勿只跑此 SQL）

-- 1. 使用者
CREATE TABLE IF NOT EXISTS users (
    user_id          SERIAL PRIMARY KEY,
    username         VARCHAR(50) UNIQUE NOT NULL,
    password_hash    TEXT NOT NULL,               -- pbkdf2_sha256: salt$hash（不存明文）
    display_name     VARCHAR(50),
    role             VARCHAR(10) NOT NULL DEFAULT 'user'
                         CHECK (role IN ('admin','user')),
    telegram_chat_id VARCHAR(30),                 -- 綁定後 Bot 指令自動歸戶
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- 2. 自選清單（每人可多組、可命名，如「AI伺服器觀察」「幫媽媽記的」）
CREATE TABLE IF NOT EXISTS watchlists (
    list_id    SERIAL PRIMARY KEY,
    user_id    INT NOT NULL REFERENCES users(user_id),
    list_name  VARCHAR(50) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, list_name)
);

CREATE TABLE IF NOT EXISTS watchlist_items (
    list_id      INT NOT NULL REFERENCES watchlists(list_id) ON DELETE CASCADE,
    stock_id     VARCHAR(10) NOT NULL REFERENCES stocks(stock_id),
    note         TEXT,
    target_price NUMERIC(12,2),
    last_signal  TEXT,
    signal_date  DATE,
    added_date   DATE DEFAULT CURRENT_DATE,
    PRIMARY KEY (list_id, stock_id)
);

-- 3. 持倉歸戶 + 帳本標籤（幫他人記：同一使用者可分「我的」「媽媽的」…）
ALTER TABLE positions ADD COLUMN IF NOT EXISTS user_id INT REFERENCES users(user_id);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS account_label VARCHAR(50) DEFAULT '我的';

CREATE INDEX IF NOT EXISTS idx_positions_user ON positions (user_id, status);
