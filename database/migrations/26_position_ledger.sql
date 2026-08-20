-- Persist actual paper-trade quantity and cash P&L per position.
ALTER TABLE positions ADD COLUMN IF NOT EXISTS shares INTEGER;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS entry_cost NUMERIC(18,2);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS exit_proceeds NUMERIC(18,2);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS net_pnl NUMERIC(18,2);

ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_shares_positive;
ALTER TABLE positions ADD CONSTRAINT positions_shares_positive
    CHECK (shares IS NULL OR shares > 0);
