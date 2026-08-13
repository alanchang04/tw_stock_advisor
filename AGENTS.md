# Project Agent Contract

Before changing this repository, read `docs/AI_COLLABORATION_PLAYBOOK.md` and
the role-specific instructions for the agent you are running.

## Shared rules

- Never let two agents edit the same checkout. Use one Git worktree and one
  branch per agent.
- Treat `data/raw/` and `data/research_versions/` as immutable inputs outside
  the Data Authority worktree. Strategy agents must not download, repair, or
  rewrite source data.
- A research result is reproducible only when it records the Git commit, data
  release ID, input-manifest hashes, strategy-spec hash, and command line.
- Do not inspect holdout performance until the documented data and engine gates
  have passed. Signal-count and correctness diagnostics are allowed.
- Preserve unrelated user work. Stage explicit paths; never use `git add -A`
  in a dirty checkout.
- Commit generated reports only when the active assignment explicitly lists
  them as deliverables.

## Current ownership

**Updated 2026-08-13: the Codex/Claude split is void — Codex ran out of quota.**

- Codex: **inactive**. No longer Data Authority, no longer integration owner.
- Claude: owns data, engine research, and integration. Two Claude instances run
  on two machines; the shared-rules section above still binds them.

Data-authority *rules* survive the owner going away. Raw data and snapshots stay
immutable, releases stay manifest-identified, and a machine that did not produce
a raw file still verifies it by manifest instead of re-downloading — gzip bytes
differ per machine, so re-downloading breaks cross-machine identity.

### Two Claude instances, one remote

`agent/swing-margin-research` is the coordination branch and both instances push
to it. Before starting work: `git fetch` and check whether the other instance
pushed. `app.py` has been touched by both instances on the same day; treat it as
a shared file and re-check it before editing.

The dated assignment history is in `docs/AI_COLLABORATION_PLAYBOOK.md`; entries
naming Codex as owner are historical and no longer binding.
