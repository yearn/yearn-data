# Powerglove and Neon branch stack

Prepared October 9, 2026. All branches below are local; this organization did not
push branches, create pull requests, or change existing GitHub PR bases.

## Review order

The foundation is `ross/tvl-history` at `cd0e120`, already represented by
[PR #12](https://github.com/yearn/yearn-metrics/pull/12). Earlier historical
indexing, pricing and accounting PR branches are preserved.

The numeric branch labels indicate local review order, not assigned PR numbers.
Each new branch contains the preceding row as an ancestor; review its diff
against that immediate base.

| Branch | Immediate base | Tip | Review scope |
| --- | --- | --- | --- |
| `ross/pr-13-powerglove-fees` | `ross/tvl-history` | `5986b9d` | Selected fee/earnings API, weekly buckets and vault filters |
| `ross/pr-14-powerglove-tvl` | `ross/pr-13-powerglove-fees` | `4dff58c` | TVL API integration, historical exclusions, quote rejection and chart reuse |
| `ross/pr-15-neon-storage` | `ross/pr-14-powerglove-tvl` | `55e7db7` | PostgreSQL storage/migration and prepared TVL history |
| `ross/pr-16-neon-operations` | `ross/pr-15-neon-storage` | `cab174c` | Staging selection, bounded updater, published-only serving and service definitions |
| `ross/pr-17-tvl-price-repairs` | `ross/pr-16-neon-operations` | `f160226` | Additional rejected quotes and incremental prepared-history repair |
| `ross/pr-18-powerglove-analytics` | `ross/pr-17-tvl-price-repairs` | `b77623a` | Owned DefiLlama references/membership, fee stacks, profitability and API routing |
| `ross/pr-19-yvusd-fees` | `ross/pr-18-powerglove-analytics` | `e0896bc` | Receipt-bound locker redistribution separated from fee revenue |

Example local review:

```bash
git diff ross/pr-18-powerglove-analytics...ross/pr-19-yvusd-fees
```

Existing PR #12 is from the `rossgalloway/yearn-data` fork into
`yearn/yearn-metrics:main`. GitHub PR bases must exist in the target repository.
For incremental upstream stacked PRs, the base branches above must first be
published in that target repository. Otherwise merge the foundation and open
subsequent layers in order against the updated upstream main, adjusting ancestry
if upstream uses squash merges. Do not open all layers against main and describe
the resulting cumulative diffs as isolated changes.

## Working checkouts

- `/home/dev/raaaws/yearn-data`: `ross/powerglove-accounting` remains the combined
  integration checkout. It contains all product layers, the separately committed
  local editor setting, and this documentation. Use the focused branches for PRs.
- `/home/dev/raaaws/worktrees/yearn-data-neon`: `ross/neon-postgres` remains at
  `cab174c`, with its operations changes committed. Its running API remains a
  distinct deployment; committing/organizing branches does not retarget services.
- Powerglove's frontend worktree and branches were not modified by this operation.

The combined integration keeps both analytics routing and published-only TVL
serving. The merged test file retains both independent regression cases.

## Recovery and cleanup

Original committed worktree tips are retained as:

- `archive/20261009/accounting-before-stack` (`5934891`)
- `archive/20261009/neon-before-stack` (`cab174c`)

Eleven obsolete unoccupied local branches were removed after their exact tips
were tagged under `archive/20261009/<original-branch-name>`:

- `codex/add-fees-data`
- `codex/defillama-rate-limit-retry`
- `feat/add-yearn-prices`
- `ross/archive-stack-746a582`
- `ross/archive-stack-before-format-cleanup`
- `ross/archive-stack-before-test-review`
- `ross/backup-powerglove-before-shared-db-rebase-20261007`
- `ross/backup-powerglove-before-tvl-rebase-20261007`
- `ross/history-coverage`
- `ross/history-source-routing`
- `ross/history-sync`

To restore a local branch, for example:

```bash
git branch ross/history-sync archive/20261009/ross/history-sync
```

A pre-cleanup bundle is retained locally at
`/tmp/yearn-stack-organize/before-cleanup.bundle`; the durable recovery references
are the Git tags. Existing PR branches, older occupied worktrees, local databases,
ignored environment files and unfinished experiments were retained.

## Validation

- Accounting checkout before integration: 525 passed, 19 skipped.
- Neon operations checkout: 501 passed, 20 skipped.
- Combined product stack: 528 passed, 20 skipped.
- Hosted PostgreSQL integration: 16 passed in disposable test schemas; production
  data was not changed by these tests.
- The final product tree equals the integration tree except for its local editor
  setting and this stack documentation.
