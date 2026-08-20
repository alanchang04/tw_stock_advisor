# Claude Assignment: MOM-1 Engine Readiness

> **Updated 2026-08-13 — Codex is out of quota; the two-agent split is void.**
> Integration is no longer owned by Codex. The branch restriction below is
> relaxed: use the coordination branch `agent/swing-margin-research`, or a
> feature branch when the work is separable.
>
> **Two Claude instances still run on two machines.** `git fetch` before
> starting and check what the other instance pushed. `app.py` is a shared file.
> Never force-push the coordination branch.
>
> **The boundaries below still bind**, including the holdout ban. They exist
> because looking at holdout returns destroys the evidence, not because Codex
> was watching.
>
> **Item 1 is already done** — `analyze_momentum_signal_overlap.py` now uses the
> D1 PIT security master. Items 2–4 continue; `reports/MOM1_ENGINE_READINESS.md`
> already exists and should be extended rather than replaced.

Read `AGENTS.md`, `docs/AI_COLLABORATION_PLAYBOOK.md`, and
`docs/SPEC_REVERSAL_AND_MULTI_STRATEGY.md` before starting.

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
- Changes are committed to `agent/swing-margin-research` (or a feature branch
  merged into it). The former "do not merge into the Data Authority branch"
  rule no longer applies — there is no Data Authority.
