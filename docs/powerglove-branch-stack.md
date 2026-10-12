# Powerglove and Neon branch stack

Updated October 12, 2026. The recent review stack uses source branches in
`yearn/yearn-metrics`. A PR's **head/source** contains the proposed changes;
its **base/target** receives them.

## Recent review order

| PR | Head / source branch | Base / target branch | Scope |
| --- | --- | --- | --- |
| [#38](https://github.com/yearn/yearn-metrics/pull/38) | `ross/powerglove-hosted-api` | `main` | Hosted Powerglove API and the integrated backend foundation |
| [#41](https://github.com/yearn/yearn-metrics/pull/41) | `ross/canonical-history` | `ross/powerglove-hosted-api` | Preserve one verified record per historical key and retire superseded publications |
| [#42](https://github.com/yearn/yearn-metrics/pull/42) | `ross/tvl-overlaps` | `ross/canonical-history` | Dated TVL graphs, overlap fixes, and the product/current fee changes from merged PR #39 |
| [#40](https://github.com/yearn/yearn-metrics/pull/40) | `ross/curation-tvl-corrections` | `ross/tvl-overlaps` | Curation scope corrections, additional overlap mappings, and owned DefiLlama comparison evidence |

Merge in this order: **38 → 41 → 42 → 40**. Each recent source branch contains
its immediate target as an ancestor. If a parent is squash-merged, restack its
children onto the resulting target before merging them.

PR [#39](https://github.com/yearn/yearn-metrics/pull/39) was merged into
`ross/tvl-overlaps`, not `main`. Its source, `ross/ybold-fee-classification`,
ends at `6136221`. The parent merge is `a08fbe3`; PR #42 carries that retained
fee work toward main. PR #39 stays merged and is not recreated.

Canonical history is at `9a3aaf6`, one commit above the hosted API tip
`bf9e902`. The curation branch has been restacked onto `a08fbe3`, including
all five existing curation/comparison commits and the three latest fee-view
commits. Its only rebase conflict was the analytics methodology version;
the combined backend retains `powerglove-analytics-5`.

For an incremental review, use the actual immediate base:

```bash
git diff origin/ross/canonical-history...origin/ross/tvl-overlaps
git diff origin/ross/tvl-overlaps...origin/ross/curation-tvl-corrections
```

## Working checkouts and names

- `/home/dev/raaaws/yearn-data`: `ross/curation-tvl-corrections`, tracking the
  source of PR #40. This is the current combined recent-work checkout.
- `/home/dev/raaaws/worktrees/yearn-data-effective-fee-products`:
  `ross/ybold-fee-classification`, retaining the merged PR #39 snapshot at
  `6136221`. The former local alias `ross/effective-fee-products` was removed
  after preserving its exact tip. The worktree path stays the same.
- Local `ross/tvl-overlaps` now matches its remote at `a08fbe3`.
- Local `ross/canonical-history` tracks the source of PR #41.
- Older occupied staging, Neon and historical-ingestion worktrees are retained.
  Some older local branches diverge from their PR refs after the unpublished
  October 9 rebase. Do not force-push them merely to clear an ahead/behind label.

Start new work from the current combined branch rather than continuing a merged
PR's snapshot. Git branches, running services and selected database publications
are independent: this stack repair does not deploy code or change hosted data.

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
