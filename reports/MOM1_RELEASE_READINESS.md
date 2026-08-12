# MOM1 named-release integration readiness

Date: 2026-08-12 (Asia/Taipei)

This is the integration-owner update to the historical Claude MOM1-0 handoff in
`reports/MOM1_ENGINE_READINESS.md`.

## Outcome

- Claude's signal-only engine was reviewed and integrated.
- Strategy code commit: `31594635f38f5644547a68c873bf94c4d1570417`.
- Data release: `tw_stock_data_2005_2014_r1`.
- Deterministic diagnostic: `reports/mom1_release_diagnostic.json`.
- Diagnostic SHA-256:
  `48E705D1521282C03DF97BDD2857CCA67716CBB197D971790E080C4A4A64F81F`.
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
2. D6 covers TWSE `punish` disposition events; historical stop-trading and
   full-delivery flags are not yet complete.
3. The 3-name/30% sector cap cannot be formally applied in the 28 affected
   months with missing PIT industry.
4. Sizing, executable T+1 price/cost handling, volume caps, and broker
   whole-lot/odd-lot splitting remain unimplemented.

## Deployment-machine verification

After verifying/extracting the data release, run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\scripts\verify_mom1_release_diagnostic.ps1
```

Success requires `passed: true`, the exact diagnostic SHA above, and
`performance_inspected: false`.
