-- Fully independent short-term margin-wash reversal strategy.
CREATE TABLE IF NOT EXISTS margin_reversal_signals (
    id BIGSERIAL PRIMARY KEY,
    stock_id VARCHAR(10) NOT NULL REFERENCES stocks(stock_id),
    signal_date DATE NOT NULL,
    score NUMERIC(10,4) NOT NULL,
    market_stress BOOLEAN NOT NULL,
    margin_wash BOOLEAN NOT NULL,
    price_oversold BOOLEAN NOT NULL,
    fundamental_ok BOOLEAN NOT NULL,
    institutional_ok BOOLEAN NOT NULL,
    features JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(stock_id, signal_date)
);

CREATE TABLE IF NOT EXISTS margin_reversal_accounts (
    id BIGSERIAL PRIMARY KEY,
    strategy_key VARCHAR(40) NOT NULL UNIQUE DEFAULT 'margin_reversal',
    initial_capital NUMERIC(18,2) NOT NULL CHECK(initial_capital > 0),
    cash NUMERIC(18,2) NOT NULL CHECK(cash >= 0),
    last_nav NUMERIC(18,2),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS margin_reversal_positions (
    id BIGSERIAL PRIMARY KEY,
    signal_id BIGINT REFERENCES margin_reversal_signals(id),
    stock_id VARCHAR(10) NOT NULL REFERENCES stocks(stock_id),
    entry_date DATE NOT NULL,
    entry_price NUMERIC(12,2) NOT NULL,
    shares INTEGER NOT NULL CHECK(shares > 0),
    entry_cost NUMERIC(18,2) NOT NULL,
    status VARCHAR(10) NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    exit_date DATE, exit_price NUMERIC(12,2), exit_reason TEXT,
    exit_proceeds NUMERIC(18,2), net_pnl NUMERIC(18,2),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(stock_id, entry_date)
);

CREATE INDEX IF NOT EXISTS idx_margin_reversal_signal_date
    ON margin_reversal_signals(signal_date, score DESC);
CREATE INDEX IF NOT EXISTS idx_margin_reversal_position_status
    ON margin_reversal_positions(status, entry_date);

CREATE TABLE IF NOT EXISTS margin_reversal_orders (
    id BIGSERIAL PRIMARY KEY,
    side VARCHAR(4) NOT NULL CHECK(side IN ('buy','sell')),
    stock_id VARCHAR(10) NOT NULL REFERENCES stocks(stock_id),
    signal_id BIGINT REFERENCES margin_reversal_signals(id),
    position_id BIGINT REFERENCES margin_reversal_positions(id),
    signal_date DATE NOT NULL,
    status VARCHAR(10) NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','filled','cancelled')),
    fill_date DATE, fill_price NUMERIC(12,2), reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_margin_reversal_pending_order
    ON margin_reversal_orders(stock_id, side) WHERE status='pending';

CREATE TABLE IF NOT EXISTS margin_reversal_studies (
    id BIGSERIAL PRIMARY KEY,
    run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    start_date DATE, end_date DATE, horizon INTEGER NOT NULL,
    treated_n INTEGER NOT NULL, control_n INTEGER NOT NULL,
    result JSONB NOT NULL
);
