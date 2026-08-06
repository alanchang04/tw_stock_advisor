-- Opt-in swing risk experiments. Formal strategy defaults remain unchanged.
ALTER TABLE positions ADD COLUMN IF NOT EXISTS stop_price NUMERIC(12,2);

ALTER TABLE pending_orders ADD COLUMN IF NOT EXISTS size_scale NUMERIC(8,4) NOT NULL DEFAULT 1.0;
ALTER TABLE pending_orders ADD COLUMN IF NOT EXISTS order_kind VARCHAR(20) NOT NULL DEFAULT 'normal';

CREATE TABLE IF NOT EXISTS swing_reentry_watch (
    id                BIGSERIAL PRIMARY KEY,
    stock_id          VARCHAR(10) NOT NULL REFERENCES stocks(stock_id),
    prior_position_id BIGINT REFERENCES positions(id),
    exit_date         DATE NOT NULL,
    exit_price        NUMERIC(12,2) NOT NULL,
    exit_reason       TEXT NOT NULL,
    status            VARCHAR(12) NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active','queued','reentered','expired')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_swing_reentry_active
    ON swing_reentry_watch(stock_id) WHERE status IN ('active','queued');

ALTER TABLE pending_orders ADD COLUMN IF NOT EXISTS reentry_watch_id BIGINT
    REFERENCES swing_reentry_watch(id);
