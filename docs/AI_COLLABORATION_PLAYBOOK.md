# AI Collaboration Playbook

Date: 2026-08-12（**2026-08-13 起部分失效，見下方狀態說明**）

Coordination branch: `agent/swing-margin-research`

## 2026-08-13 狀態更新：Codex 額度用盡

下面的 Codex／Claude 分工表、worktree 建立步驟與「Codex reviews and integrates」
交接流程**已失效**，保留作為歷史紀錄。實際現況：

- Codex 不再運作；資料權責與整合改由 Claude 承擔。
- 兩台機器各有一個 Claude 實例，**都推同一條協調分支**。
- DATA-1／DATA-2／DATA-3 已完成（D4/D5、處置回補、`tw_stock_data_2005_2014_r2`）。
- Claude 的 MOM1-0 已交付 `reports/MOM1_ENGINE_READINESS.md`；F0 執行稽核亦已提交。

**仍然有效且不因 Codex 離開而放寬的**：資料釋出閘門（§Data release gate）、
`usage_policy.blocked` 的 holdout 禁令、不可變 raw／snapshot、
以及「不同機器靠 manifest 驗證而非重新下載」。這些是為了資料完整性，
不是為了配合某個 agent 的存在。

## Operating decision

Codex and Claude can work in parallel now, but they must not share a mutable
checkout. The minimal shared environment is:

1. one Git repository and remote as the source of code truth;
2. one worktree and branch per agent;
3. one Data Authority for raw data and snapshot publication;
4. immutable data releases identified by manifests and hashes; and
5. committed assignment/readiness documents as the handoff channel.

This shares files, data identity, and task state. It does not share the agents'
conversation memory or create direct model-to-model messaging.

## Work allocation

| Owner | Work package | Immediate outcome |
| --- | --- | --- |
| Codex | DATA-1: finish the in-progress D4/D5 and execution-action ledger work without mixing unrelated files | Reviewed code/tests, committed manifests, and a clean integration commit |
| Codex | DATA-2: harden TWSE disposition backfill | Deterministic gzip bytes, tests, official `punish` history first; `notice` is a separately reported gap |
| Codex | DATA-3: publish a reproducible data release | Release descriptor with file counts, SHA-256 values, coverage, schema/version, and known gaps |
| Claude | MOM1-0: engine correctness and PIT-universe audit | Tested signal-only engine and `reports/MOM1_ENGINE_READINESS.md`, with no holdout metrics |

Codex owns integration because the current Data Authority checkout already has
uncommitted data-pipeline work. Claude must not touch those files unless a later
assignment transfers ownership explicitly.

## Data release gate

Strategy research may use a dataset only after Codex publishes a release
descriptor that contains all of the following:

- unique `data_release_id`;
- source and normalized-snapshot manifest paths;
- SHA-256 for each manifest and material derived artifact;
- date and market coverage;
- schema or builder version and Git commit;
- known gaps, including disposition `punish` and `notice` status; and
- a verification command that passes from a second worktree.

Until then, Claude may build and test engine logic with fixtures and inspect
signal counts, but may not treat local mutable data as a research result.

The D3/D4/D5 manifests and implementations are committed and included in the
published component release below. Their component-level blockers remain
binding; inclusion proves identity and availability, not strategy readiness.

### Published component release

`tw_stock_data_2005_2014_r1` is the current immutable cross-machine component
release. Its descriptor is
`reports/data_releases/tw_stock_data_release_2005_2014_r1.json` and deployment-
machine instructions are in `docs/DEPLOYMENT_MACHINE_DATA_RELEASE.md`.

This release is approved for engine correctness, PIT signal counts, and
disposition/universe verification. It does not open backward holdout returns;
the descriptor's `known_gaps` and `usage_policy.blocked` remain binding.

## Create the Claude worktree

Run these commands from the repository root after pulling this coordination
commit:

```powershell
git fetch origin
$repoPath = (Resolve-Path .).Path
$claudePath = Join-Path (Split-Path $repoPath) "tw_stock_advisor_claude"
git worktree add $claudePath -b research/mom1-engine origin/agent/swing-margin-research
```

Claude can begin fixture-based MOM1-0 work immediately in that worktree. Do not
point it at the mutable Data Authority directories until DATA-3 is published.

After DATA-3, the simplest same-machine data sharing is a directory junction
from the Claude worktree to the canonical data directories. This is read-only
by project policy even though a Windows junction does not enforce permissions:

```powershell
New-Item -ItemType Directory -Path (Join-Path $claudePath "data") -Force
New-Item -ItemType Junction -Path (Join-Path $claudePath "data\raw") -Target (Join-Path $repoPath "data\raw")
New-Item -ItemType Junction -Path (Join-Path $claudePath "data\research_versions") -Target (Join-Path $repoPath "data\research_versions")
```

If an agent needs to mutate data, it must do so in a private staging directory
and hand the proposed artifact back to Codex for release. Never mutate a shared
release in place.

## Handoff protocol

Each agent ends a work package with one commit and a short report containing:

- branch and commit SHA;
- files changed;
- commands/tests run and their results;
- data release ID, or `fixtures-only`;
- blockers and decisions needed; and
- explicit confirmation that holdout metrics were or were not inspected.

Claude pushes its branch; Codex reviews and integrates it only after checking
the report and rerunning the targeted tests. Neither agent force-pushes a shared
branch.

## When to build more infrastructure

Do not build an MCP server or custom multi-agent orchestrator yet. Git branches,
worktrees, manifests, and committed handoff reports cover the current need with
low operational risk. Add a task database or message broker only if manual Git
handoffs become the measured bottleneck.
