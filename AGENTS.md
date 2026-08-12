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

- Codex: Data Authority and integration owner.
- Claude: MOM-1 strategy-engine research in a separate worktree.

The dated assignment and acceptance criteria are in
`docs/AI_COLLABORATION_PLAYBOOK.md`.
