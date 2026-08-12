# MOM1 named-release integration readiness

Date: 2026-08-12 (Asia/Taipei)

This is the integration-owner update to the historical Claude MOM1-0 handoff in
`reports/MOM1_ENGINE_READINESS.md`.

## Outcome

- Claude's signal-only engine was reviewed and integrated.
- Strategy code commit: `31594635f38f5644547a68c873bf94c4d1570417`.
- Data release: `tw_stock_data_2005_2014_r2` (supersedes `r1`; adds the
  `twse_altered_trading_2005_2014` component, 9 components in total).
- Deterministic diagnostic: `reports/mom1_release_diagnostic.json`.
- Diagnostic SHA-256:
  `29CB56DA16F9D26417EDF630B68B8E868E5AECE5A9E97805AF014D617865211B`.
- The same command was run twice locally and produced byte-identical output.
- Holdout performance inspected: **no**. No return, NAV, Sharpe, drawdown, win
  rate, or parameter comparison was calculated.

The adapter verifies the release descriptor, component manifest and quality
hashes, component content hashes, and the eight material parquet files consumed
by MOM1 before loading them.

## Signal-count findings

| Item | Result |
| --- | ---: |
| Price coverage | 2005-01-03 to 2014-12-31 |
| Trading sessions / stock IDs | 2,469 / 1,042 |
| Decision months | 120 |
| Months with a non-empty eligible universe | 108 |
| Median eligible count before / after disposition | 333.5 / 333.0 |
| D6 disposition events | 604 |
| Restricted stock-sessions | 3,538 |
| Months where disposition removed a month-end candidate | 40 |
| Total month-end disposition exclusions | 53 |
| PIT master vs monthly-history mismatch months | 0 |
| Months where selected names still lack PIT industry | 28 |

These are membership and signal counts only. They do not reveal holdout returns.

## Frozen implementation choices

- Liquidity requires all 20 sessions to be non-null.
- The liquidity percentile denominator is all PIT TWSE common stocks with a
  complete window, before price/history filters.
- Entry/retention quotas use floor at 10%/20%; zero slots means cash.
- Existing holdings still in the top 20% have priority under the 10-position
  cap; top-10% entrants fill remaining slots.
- Ties use signal descending, then `stock_id` ascending.

These choices are now written into
`docs/SPEC_DATA_FOUNDATION_AND_MOMENTUM.md`, so they cannot be changed after
seeing performance.

## Remaining blockers

F0 overall is **not passed**, and the 2008-2014 backward holdout remains closed:

1. D3 is structurally usable for signal adjustment, but the actual-share
   execution ledger and unresolved reference resets still block performance.
2. TWSE stop-trading (停止交易) has no separate official flag; it is currently
   inferred only from a missing quote on that session.
3. The named release has no official locked-limit state for fill validation.

Full-delivery/altered-trading is **no longer a blocker**: r2 adds
`twse_altered_trading_2005_2014` (2,469/2,469 trading days, 53,159 observations,
209 securities), and the loader now merges it with the disposition frame.
The two exclusion sources are kept as separate matrices
(`disposition_restricted`, `altered_trading_restricted`) plus their union
(`restricted`), so every exclusion can still be attributed to a specific rule.

The shared fee/tax/slippage constants are no longer a blocker: `ca29852`
imports `FEE_RATE`, `TAX_RATE`, `buy_fill`, and `sell_fill` from
`agent/strategy.py` into the deterministic order ledger, so MOM1 and the
existing strategy now use one cost definition instead of two. The three
blockers above are exactly the `remaining_blockers` recorded in
`reports/mom1_f0_execution_readiness.json`.

## F0 execution package update

Strategy contract commit: `ca2985292a3f77e68d2546cc673baaf974cde7cc`.
The package now implements and tests:

- PIT industry lookup with no future snapshots;
- 3-name/30% industry cap across the full buffered candidate list;
- a frozen fail-closed rule: missing/non-PIT industry makes that stock ineligible;
- 10% equal-weight integer-share sizing and the 1% average-volume cap;
- explicit common-lot and odd-lot broker quantities;
- T+1-or-later execution attempts, side-specific locked-limit behavior, and
  defer/cancel states without invented fills;
- a deterministic cost ledger that records the raw open, the executable price
  after slippage, gross notional, commission, transaction tax, and the resulting
  cash delta per order, using the shared `agent/strategy.py` constants.

The deterministic F0 diagnostic is
`reports/mom1_f0_execution_readiness.json`, SHA-256
`95751DC46196FB2837029D4AA64D0F0EFA225F9CDCDEB3C2AD617C1AF9DCD8DD`.
Across 108 active decision months, the formal industry policy produced 1,080
selected stock-months; 1,070 had a usable T+1 open and complete sizing inputs.
The ten unavailable opens remain unfilled rather than being replaced by a
close or forward-filled price.

F0 remains blocked only by release/integration inputs: official locked-limit
state, TWSE stop-trading flags, and D3 execution-ledger reconciliation.
All three are data gaps owned by the Data Authority, not code defects.
Industry missingness no longer blocks the whole month because its conservative
exclusion policy was frozen before any performance inspection.

## When strategy effects may be inspected

Signal membership and operational behavior are inspectable now. Historical
effectiveness is intentionally not inspectable yet. The backward holdout opens
once all of the following are true:

1. a new immutable release resolves or formally disposes the remaining D3 and
   TWSE execution-restriction blockers;
2. cost-ledger integration and deterministic order-list replay pass F0;
3. the two registered variants (MOM-1A and MOM-1B) and all acceptance thresholds
   remain unchanged; and
4. the one-time F1 command is committed before execution.

At that point F1 is run once and reports the strategy effects. No preliminary
return peek is allowed before these gates.

## Deployment-machine verification

After verifying/extracting the data release, run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\scripts\verify_mom1_release_diagnostic.ps1
```

Success requires `passed: true`, the exact diagnostic SHA above, and
`performance_inspected: false`.

Then verify the new portfolio/execution mechanics:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\scripts\verify_mom1_f0_execution.ps1
```

This second check currently reports `f0_status: blocked` by design and lists
the remaining data/integration gates; its output must still match the committed
SHA exactly.
