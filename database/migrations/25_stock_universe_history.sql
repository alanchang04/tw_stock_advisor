-- Point-in-time stock universe snapshots.
CREATE TABLE IF NOT EXISTS stock_universe_history (
    snapshot_date   DATE        NOT NULL,
    stock_id        VARCHAR(20) NOT NULL REFERENCES stocks(stock_id),
    market          VARCHAR(10) NOT NULL,
    industry_code   VARCHAR(20),
    asset_type      VARCHAR(20) NOT NULL DEFAULT 'common_stock',
    listing_date    DATE,
    delisting_date  DATE,
    is_active       BOOLEAN     NOT NULL,
    PRIMARY KEY (snapshot_date, stock_id)
);

CREATE INDEX IF NOT EXISTS idx_universe_history_stock_date
    ON stock_universe_history (stock_id, snapshot_date DESC);
