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
  `10ADCED39BC0926E0368071C3E72329918E7ABB204C734371258E55E651154CE`.
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

## F0 status: passed (2026-08-13)

Every `SPEC_DATA_FOUNDATION_AND_MOMENTUM.md` §9.1 correctness item now has test
coverage, and both previously recorded blockers are closed with measured
evidence. The diagnostic carries the item-to-test map in `spec_9_1_coverage`.

This asserts **implementation correctness only**. It reveals no holdout
performance, and per §9.2 the backward holdout is a falsification test that can
never establish that the strategy works.

### Closed blocker 1 — TWSE stop-trading flags

Measured immaterial rather than fixed. The official `TWTAWU` history only starts
2011-10-03, so it can never be completed for 2005-2011. In the covered window all
28 suspensions are foreign primary listings, TDRs or warrants; 9 of the 11
common-stock events have no quote at all (already excluded by the existing
`price_present` test) and the 2 with quotes both close below NT$10 (excluded by
the §7.1.4 price floor). **The intersection with MOM-1's 239 selected securities
is empty.**

Residual risk, stated plainly: 2005 to 2011-09 cannot be verified. The structural
argument (suspended → no quote → excluded) is period-independent, but that is a
reasoned extrapolation, not proof.

### Closed blocker 2 — D3 actual-share execution ledger

Resolved by frozen policy, §7.5.2. Only **10 of the 653** blocked events fall
inside MOM-1 holding windows (10 of 1,070 stock-months, 0.93%). Nine are optional
rights issues handled by a never-subscribe policy that needs none of the missing
terms and is conservative in direction; the remaining one is a mandatory stock
dividend, force-closed at the pre-event close, affecting 0.09% of stock-months.

Locked-limit state is **no longer a blocker**. The official feed has no such
field, so this was never a download problem; it is now a rule frozen in
`SPEC_DATA_FOUNDATION_AND_MOMENTUM.md` §7.5.1 and implemented in
`research/twse_price_limits.py`. A `change_pct` threshold was measured and
rejected: across 1,917,768 rows the 6.5–7.0% region is smooth with no
discontinuity, because limit prices must land on a legal tick and cross bands
(a 9.99 reference gives a 10.65 limit, only +6.61%), so a 6.9% cut would miss
54% of genuine limit-up sessions. The frozen rule instead recovers the official
reference price from `change_pct`, applies the official tick table, and requires
both an exact limit close **and** a single-price session
(`open == high == low == close`). In the F0 run this blocks 6 stock-months.

Full-delivery/altered-trading is **no longer a blocker**: r2 adds
`twse_altered_trading_2005_2014` (2,469/2,469 trading days, 53,159 observations,
209 securities), and the loader now merges it with the disposition frame.
The two exclusion sources are kept as separate matrices
(`disposition_restricted`, `altered_trading_restricted`) plus their union
(`restricted`), so every exclusion can still be attributed to a specific rule.

The shared fee/tax/slippage constants are no longer a blocker: `ca29852`
imports `FEE_RATE`, `TAX_RATE`, `buy_fill`, and `sell_fill` from
`agent/strategy.py` into the deterministic order ledger, so MOM1 and the
existing strategy now use one cost definition instead of two.
`remaining_blockers` in `reports/mom1_f0_execution_readiness.json` is now empty;
the two entries above appear there as `closed_blockers` with their evidence.

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
`0A46959ECA385C4A53D399458976EF9035C80D6F21B7CA52EB64EAF45CC40D1D`.
Across 108 active decision months, the formal industry policy produced 1,080
selected stock-months; 1,070 had a usable T+1 open, of which 6 were blocked by
a locked limit-up session, leaving 1,064 with complete sizing inputs. The ten
unavailable opens and the six locked sessions remain unfilled rather than being
replaced by a close or forward-filled price.

Industry missingness no longer blocks the whole month because its conservative
exclusion policy was frozen before any performance inspection.

## When strategy effects may be inspected

Signal membership and operational behavior are inspectable now. Historical
effectiveness is intentionally not inspectable yet. The backward holdout opens
once all of the following are true:

1. ~~a new immutable release resolves the remaining D3 and TWSE
   execution-restriction blockers~~ — **done**: r2 plus the frozen policies in
   §7.5.1 and §7.5.2;
2. ~~cost-ledger integration and deterministic order-list replay pass F0~~ —
   **done**: `f0_status: passed`;
3. the two registered variants (MOM-1A and MOM-1B) and all acceptance thresholds
   remain unchanged; and
4. the one-time F1 command is committed before execution.

Items 1 and 2 are satisfied. **F1 may now be opened once**, subject to items 3
and 4, and its result must be recorded whatever it says.

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

This second check reports `f0_status: passed` and carries the §9.1 item-to-test
map plus the two closed blockers with their evidence; its output must still
match the committed SHA exactly.
