-- Account-level cash/NAV ledger for the original swing strategy.
CREATE TABLE IF NOT EXISTS paper_accounts (
    id              BIGSERIAL PRIMARY KEY,
    strategy_key    VARCHAR(40) NOT NULL UNIQUE,
    initial_capital NUMERIC(18,2) NOT NULL CHECK (initial_capital > 0),
    cash            NUMERIC(18,2) NOT NULL CHECK (cash >= 0),
    last_nav        NUMERIC(18,2),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS paper_account_transactions (
    id          BIGSERIAL PRIMARY KEY,
    account_id  BIGINT NOT NULL REFERENCES paper_accounts(id),
    position_id BIGINT REFERENCES positions(id),
    trade_date  DATE NOT NULL,
    kind        VARCHAR(20) NOT NULL CHECK (kind IN ('deposit','buy','sell','adjustment')),
    amount      NUMERIC(18,2) NOT NULL,
    cash_after  NUMERIC(18,2) NOT NULL CHECK (cash_after >= 0),
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_paper_account_tx_date
    ON paper_account_transactions (account_id, trade_date, id);

ALTER TABLE positions ADD COLUMN IF NOT EXISTS paper_account_id BIGINT
    REFERENCES paper_accounts(id);
