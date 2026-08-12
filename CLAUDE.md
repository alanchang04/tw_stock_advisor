# Claude Assignment: MOM-1 Engine Readiness

Read `AGENTS.md`, `docs/AI_COLLABORATION_PLAYBOOK.md`, and
`docs/SPEC_REVERSAL_AND_MULTI_STRATEGY.md` before starting.

Work only in branch `research/mom1-engine` and in a checkout that is separate
from the Codex Data Authority checkout.

## Assignment

1. Audit `scripts/analyze_momentum_signal_overlap.py`. Replace the current
   ticker-prefix ETF approximation with point-in-time security-master or data
   release fields. Do not silently fall back to a heuristic.
2. Extract or implement the MOM-1 signal-construction engine needed for the F0
   correctness gate: formation window, month-end decision date, next-session
   execution, liquidity screen, investability exclusions, and deterministic
   ranking/tie handling.
3. Add focused unit tests for time alignment, missing sessions, universe
   membership, filters, and prevention of look-ahead leakage.
4. Produce `reports/MOM1_ENGINE_READINESS.md` containing commands, test results,
   unresolved assumptions, and the exact data release ID used.

## Boundaries

- Do not download or modify raw data.
- Do not repair manifests or snapshots; report a data defect to the Data
  Authority instead.
- Do not calculate, reveal, or tune on holdout returns, Sharpe, CAGR, drawdown,
  or winning parameter combinations. Correctness diagnostics and signal counts
  are allowed.
- If no released point-in-time field can distinguish securities from ETFs,
  stop that part and document the missing field. Do not invent a proxy.

## Definition of done

- The new tests pass together with the existing suite relevant to the changed
  modules.
- Every diagnostic is reproducible from a named immutable data release.
- Use `tw_stock_data_2005_2014_r1` for released component diagnostics and record
  that exact ID in the readiness report.
- The readiness report clearly separates code defects, data defects, and open
  research choices.
- Changes are committed to `research/mom1-engine`; they are not merged into the
  Data Authority branch by Claude.
