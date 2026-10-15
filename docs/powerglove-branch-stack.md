# Powerglove and Neon branch stack

Updated October 15, 2026. Source branches live in `yearn/yearn-metrics`.
A PR's **head/source** contains its changes; its **base/target** receives them.

## Current review order

| PR / branch | Head / source branch | Base / target branch | Status and scope |
| --- | --- | --- | --- |
| [#38](https://github.com/yearn/yearn-metrics/pull/38) | `ross/powerglove-hosted-api` | `main` | Open draft; hosted API and integrated backend foundation |
| Follow-up | `ross/tvl-comparison-followup` | `ross/powerglove-hosted-api` | Service retirement, current adapter comparison, mapping corrections and complete-position selection |

PRs [#40](https://github.com/yearn/yearn-metrics/pull/40),
[#42](https://github.com/yearn/yearn-metrics/pull/42), and
[#41](https://github.com/yearn/yearn-metrics/pull/41) merged on October 12,
in that order. Their curation, overlap and canonical-history work is now
contained in `origin/ross/powerglove-hosted-api` at `f6d723d`.
PR [#39](https://github.com/yearn/yearn-metrics/pull/39) was merged into
`ross/tvl-overlaps` and is also included through those merges. These PRs
do not need to be reopened or replayed.

The follow-up starts at `f6d723d`. That base has the same committed file tree
as the former working branch, `ross/curation-tvl-corrections` at `f572814`;
the difference is three merge commits. Its six commits separate:

1. Legacy API service retirement and recovery documentation.
2. PostgreSQL catalog active-flag compatibility.
3. Current Yearn curation registry discovery.
4. Finance adapter contribution replay and comparison evidence.
5. Corrected overlap destinations and stale mapping suppression.
6. Complete-position selection, preserving valid zero-debt updates.

Review the follow-up against its immediate base:

```bash
git diff origin/ross/powerglove-hosted-api...ross/tvl-comparison-followup
```

It may merge into the hosted API branch before #38 merges into `main`.
If #38 lands first, retarget the follow-up to `main`, restacking if the
merge method changed ancestry.

## Working checkouts and names

- `/home/dev/raaaws/yearn-data`: `ross/tvl-comparison-followup`.
- `ross/curation-tvl-corrections` retains its merged tip at `f572814`.
- The local `ross/powerglove-hosted-api` branch is older than its remote
  tracking ref. Use `origin/ross/powerglove-hosted-api` when creating work.
- `/home/dev/raaaws/worktrees/yearn-data-effective-fee-products` retains
  `ross/ybold-fee-classification`, the merged PR #39 snapshot at `6136221`.
- Older occupied Neon and historical-ingestion worktrees remain separate.
  Do not force-push older local branches merely to clear ahead/behind labels.

Git branches, running services and selected database publications are
independent. This branch organization does not deploy or select data.
The original working tip is retained at
`refs/archive/20261015-comparison-followup/curation`.

## Earlier PRs

PRs #22–#37 retain their existing main-targeted review layout. Local branch
numbering does not match their current GitHub PR numbers. The replacement PRs
use branches in `yearn/yearn-metrics`, not the earlier fork PRs:

| Branch | Current PR |
| --- | --- |
| `ross/pr-01-envio-history` | [#22](https://github.com/yearn/yearn-metrics/pull/22) |
| `ross/pr-02-bounded-catchup` | [#23](https://github.com/yearn/yearn-metrics/pull/23) |
| `ross/pr-03-yearn-pricing` | [#24](https://github.com/yearn/yearn-metrics/pull/24) |
| `ross/pr-04-canonical-fees` | [#25](https://github.com/yearn/yearn-metrics/pull/25) |
| `ross/pr-05-verified-fee-recovery` | [#26](https://github.com/yearn/yearn-metrics/pull/26) |
| `ross/pr-06-closed-day-exports` | [#27](https://github.com/yearn/yearn-metrics/pull/27) |
| `ross/pr-07-price-fallbacks` | [#28](https://github.com/yearn/yearn-metrics/pull/28) |
| `ross/pr-08-fantom-replay` | [#29](https://github.com/yearn/yearn-metrics/pull/29) |
| `ross/tvl-history` | [#30](https://github.com/yearn/yearn-metrics/pull/30) |
| `ross/pr-13-powerglove-fees` | [#31](https://github.com/yearn/yearn-metrics/pull/31) |
| `ross/pr-14-powerglove-tvl` | [#32](https://github.com/yearn/yearn-metrics/pull/32) |
| `ross/pr-15-neon-storage` | [#33](https://github.com/yearn/yearn-metrics/pull/33) |
| `ross/pr-16-neon-operations` | [#34](https://github.com/yearn/yearn-metrics/pull/34) |
| `ross/pr-17-tvl-price-repairs` | [#35](https://github.com/yearn/yearn-metrics/pull/35) |
| `ross/pr-18-powerglove-analytics` | [#36](https://github.com/yearn/yearn-metrics/pull/36) |
| `ross/pr-19-yvusd-fees` | [#37](https://github.com/yearn/yearn-metrics/pull/37) |

These earlier branches were not rewritten by the recent-stack repair.

## Recovery and validation

Before the repair, exact local tips were saved under
`refs/archive/20261012-recent-stack/` for the curation, yBOLD, TVL-overlap,
canonical-history and former effective-fee branches. The pre-repair curation
tip is `5725078`; the former local yBOLD tip is `0f2bd41`.

Earlier recovery tags under `archive/20261009/` and the rebase references under
`refs/archive/20261009-before-main-rebase/` remain intact. To recover the former
fee alias without disturbing its occupied worktree:

```bash
git branch ross/effective-fee-products \
  refs/archive/20261012-recent-stack/ross/effective-fee-products
```

The repaired combined backend passed **652 tests**, with **23 skipped**.
Range comparison verified the five curation/comparison commits retain their
changes; only the obsolete analytics-version assignment was dropped.
