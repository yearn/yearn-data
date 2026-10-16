# Powerglove and Neon branch stack

Updated October 16, 2026. Source branches live in `yearn/yearn-metrics`.
A PR's **head/source** contains its changes; its **base/target** receives them.

## Current review order

| PR / branch | Head / source branch | Base / target branch | Status and scope |
| --- | --- | --- | --- |
| [#38](https://github.com/yearn/yearn-metrics/pull/38) | `ross/powerglove-hosted-api` | `ross/pr-19-yvusd-fees` (#37) | Open draft; hosted API and integrated backend foundation |
| [#43](https://github.com/yearn/yearn-metrics/pull/43) | `ross/tvl-comparison-followup` | `ross/powerglove-hosted-api` (#38) | Service retirement, current adapter comparison, mapping corrections and complete-position selection |

Review from #22 through #38, then #43. Each PR targets its immediate
predecessor; only #22 targets `main`. The earlier layers are listed below.

PRs [#40](https://github.com/yearn/yearn-metrics/pull/40),
[#42](https://github.com/yearn/yearn-metrics/pull/42), and
[#41](https://github.com/yearn/yearn-metrics/pull/41) merged on October 12,
in that order. Their curation, overlap and canonical-history work is now
contained in `origin/ross/powerglove-hosted-api` at `f6d723d`.
PR [#39](https://github.com/yearn/yearn-metrics/pull/39) was merged into
`ross/tvl-overlaps` and is also included through those merges. These PRs
do not need to be reopened or replayed.

The follow-up originally started at `f6d723d`. That base has the same committed file tree
as the former working branch, `ross/curation-tvl-corrections` at `f572814`;
the difference is three merge commits. Its six implementation commits separate:

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

Merge from the bottom of the stack. After a parent PR merges, retarget its
child to the parent's former base. Preserve ancestry when merging, or
restack the remaining children after a squash or rebase merge. Changes to
a lower layer must also be carried through its descendants.

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

PRs #23–#37 now target their immediate predecessor branches. Their source
commits are unchanged. Local branch numbering does not match their current
GitHub PR numbers. The replacement PRs use branches in `yearn/yearn-metrics`,
not the earlier fork PRs:

| Branch | Current PR | Base / target branch |
| --- | --- | --- |
| `ross/pr-01-envio-history` | [#22](https://github.com/yearn/yearn-metrics/pull/22) | `main` |
| `ross/pr-02-bounded-catchup` | [#23](https://github.com/yearn/yearn-metrics/pull/23) | `ross/pr-01-envio-history` |
| `ross/pr-03-yearn-pricing` | [#24](https://github.com/yearn/yearn-metrics/pull/24) | `ross/pr-02-bounded-catchup` |
| `ross/pr-04-canonical-fees` | [#25](https://github.com/yearn/yearn-metrics/pull/25) | `ross/pr-03-yearn-pricing` |
| `ross/pr-05-verified-fee-recovery` | [#26](https://github.com/yearn/yearn-metrics/pull/26) | `ross/pr-04-canonical-fees` |
| `ross/pr-06-closed-day-exports` | [#27](https://github.com/yearn/yearn-metrics/pull/27) | `ross/pr-05-verified-fee-recovery` |
| `ross/pr-07-price-fallbacks` | [#28](https://github.com/yearn/yearn-metrics/pull/28) | `ross/pr-06-closed-day-exports` |
| `ross/pr-08-fantom-replay` | [#29](https://github.com/yearn/yearn-metrics/pull/29) | `ross/pr-07-price-fallbacks` |
| `ross/tvl-history` | [#30](https://github.com/yearn/yearn-metrics/pull/30) | `ross/pr-08-fantom-replay` |
| `ross/pr-13-powerglove-fees` | [#31](https://github.com/yearn/yearn-metrics/pull/31) | `ross/tvl-history` |
| `ross/pr-14-powerglove-tvl` | [#32](https://github.com/yearn/yearn-metrics/pull/32) | `ross/pr-13-powerglove-fees` |
| `ross/pr-15-neon-storage` | [#33](https://github.com/yearn/yearn-metrics/pull/33) | `ross/pr-14-powerglove-tvl` |
| `ross/pr-16-neon-operations` | [#34](https://github.com/yearn/yearn-metrics/pull/34) | `ross/pr-15-neon-storage` |
| `ross/pr-17-tvl-price-repairs` | [#35](https://github.com/yearn/yearn-metrics/pull/35) | `ross/pr-16-neon-operations` |
| `ross/pr-18-powerglove-analytics` | [#36](https://github.com/yearn/yearn-metrics/pull/36) | `ross/pr-17-tvl-price-repairs` |
| `ross/pr-19-yvusd-fees` | [#37](https://github.com/yearn/yearn-metrics/pull/37) | `ross/pr-18-powerglove-analytics` |

These earlier branches were not rewritten by the recent-stack repair.

## October 16 ancestry reconciliation

The hosted API branch contained rewritten copies of the earlier layers,
but not their original commit IDs. All 25 commits through #37 had
patch-equivalent copies in the integration history.

Commit `e182dc6` joins #37's original head `e0896bc` to the hosted API head
`f6d723d`. Its file tree is exactly the same as `f6d723d`; the integrated
#39–#42 work is retained. Commit `5a83aaa` carries that ancestry into #43
and also preserves its existing tree. This documentation update is the
only file change made for the stack repair. No force-push is needed.

Before the repair, all 18 open PR heads and `main` were saved under
`refs/archive/20261016-stack-repair/` in the original checkout. A verified
Git bundle and the original PR descriptions are retained locally under
`artifacts/stack-repair-20261016/`. The original checkout and its unrelated
uncommitted changes were not used to prepare the new commits.

## Recovery and validation

Before the October 12 repair, exact local tips were saved under
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
