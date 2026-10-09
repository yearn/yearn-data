# Dated TVL flow graph and overlap corrections

`GET /api/tvl/graph` serves Powerglove's existing node/edge contract from the
selected Yearn Data TVL publication. It supports `chainId`, `category`
(`v1`, `v2`, `v3`, `curation`), `includeRetired=true|false` (default true), and
`datasetId`. Both the pairing server and hosted API expose it. Hosted requests
for a retired dataset follow the existing HTTP 410 / reload contract.

The response contains `snapshotAt`, `asOfTimestamp`, `datasetId`,
`totalRawTvlUsd`, `totalOverlapTvlUsd`, `totalTvlUsd`, `nodes`, `edges`, and
`bridgeExclusions`. The timestamp is the publication's stored close, not a
current RPC read. No request performs discovery, RPC collection, or pricing.

## Accounting and display conditions

- Only an included parent and child can generate a deduction. Retired-vault
  exclusion runs before accounting. Category and chain filters select headline
  contributions and graph roots, retaining connected upstream/downstream context.
- Recorded debt, shared ownership, and parent/child TVL caps use `tvl.account`.
  Synthetic intermediary nodes explain allocations but never add to aggregate TVL.
- Each holder's ownership is split across parents and children once. The graph
  merges a shared holder's child edge while retaining distinct parent allocations.
- `tvlUsd` on an edge describes flow. `deductedTvlUsd` describes the contribution
  to overlap accounting. They intentionally differ for ordinary strategies and
  curation flows. Do not sum all displayed edges to derive headline overlap;
  multi-hop context and filtered categories make that incorrect.
- Retained strategy debts can exceed a V2 parent's assets. Verified downstream
  flows receive their accounted budget first; the remaining parent TVL is split
  among other debts. Such allocation edges have
  `valueSource=parent-tvl-capped-debt-estimate`; `recordedDebtUsd` retains the
  uncapped observation. Ordinary verified debt uses `dated-strategy-debt`.
- Unpriced vaults are omitted from graph nodes and retained under
  `diagnostics.unavailableNodes`, rather than shown as zero-value vaults.
  Graph totals follow the existing API's available-amount convention;
  `diagnostics.status` identifies incomplete accounting.
- Vaults and curation retain their existing separate-layer headline policy.
  The graph can show a physical curation flow with a zero headline deduction.

## Validated report findings

The [October 9 report](https://artifacts.yearn.dev/e628f29bbdc728586939ea6aeaa226b9.md)
describes DefiLlama's own controlled comparison at Ethereum block 26,156,469.
Its $40.132M reduction cannot be subtracted from Yearn Data's different inventory,
close, prices, and existing deductions.

Read-only checks against Yearn Data's October 8, 2026 UTC close
(`1791503999`, Ethereum block `26151122`, Katana block `44761188`) established:

| Pattern | Existing behavior | Correction |
| --- | --- | --- |
| Five legacy V2 → V3 routers | Already deducted using dated debt and child shares | Retained; migrated to graph without the old service's whole-vault estimate |
| Other V2 router hops | Missing target mappings | Added 14 mappings, including the preceding USDT V2 → V2 hop; reverify `yVault()` at every collection block |
| Staked yBOLD and locked yvUSD | Already deducted | Retained |
| Ajna share wrappers | Wallet balance capped ownership even when shares were deployed | Use share-denominated `totalAssets`, bounded by child supply and accounting caps; retain idle wallet balance as evidence |
| USDC-1 → USDS-1 depositor | Already deducted; approximately $5.260M at this close | Retained, with its intermediary visible in the graph |
| yvUSD → Katana | Ordinary allocation without a known child | Verify reciprocal counterparts and remote vault at both dated blocks; deduct the smaller supported source debt / remote ownership value |

An independent withdrawal-queue scan at the report's block found 19 routes with
positive recorded debt, delegated assets, and child shares in our catalog: five
existing plus 14 newly registered. This verifies those relationships, not the
report's exact 20-route list or complete $40.132M execution. The report does not
provide its full raw relationship list or fixed-price replay inputs.

Cross-chain validation separately reproduced `REMOTE_CHAIN_ID=747474`, both
`REMOTE_COUNTERPART` links, and the remote `vault()` at Ethereum block `26156464`
and Katana block `44825516`, matching the report's named contracts.

Using the same stored close and prices, a separate local review dataset produced:

| Additional deduction | USD |
| --- | ---: |
| Newly mapped router holdings | 5,426,001.43 |
| Deployed Ajna wrapper shares, including Polygon USDC | 16,372.72 |
| yvUSD → Katana | 928,357.17 |
| Total | 6,370,731.32 |

Known external TVL changed from $236,787,808.55 to $230,417,077.24. These remain
known subtotals, not certified complete unique-capital totals. The DUSD router's
valuation is unavailable because of missing price evidence; it contributes no
invented zero deduction. No prices or catalog membership were altered.

The report's stale Optimism data, Fantom pricing differences, V1/LP coverage,
and parent-level curation scope do not establish additional deductible amounts
in this dataset. The four curation-category Katana pre-deposit vaults also retain
their existing treatment; applying new historical bridge exclusions requires
dated migration evidence for those specific contracts. They are not covered by
the existing four dated V3 bridge entries.

## Reproduce locally without changing the active publication

```bash
PYTHONPATH=src .venv/bin/python scripts/review_tvl_overlaps.py \
  --env .env --publication data/powerglove-tvl-neon \
  --out artifacts/tvl-overlap-review/new-review
```

The script reads the pinned source database and archive RPCs, saves a new
single-close `review.sqlite`, a separate publication, `validation.json`, and
`graph.json`. It refuses to overwrite an existing review database. It does not
write to Neon, activate a hosted release, or rebuild the full historical series.
Use `PYTHONPATH=src` for this checkout: its existing virtualenv may have an
editable installation pointing to another worktree.

For an actual data rollout, recollect affected dated positions into new runs,
prepare the resulting TVL history and matching analytics, validate the source →
API → consumer path, and activate the release through the existing publication
workflow. Old point-in-time balances must never be replaced with today's
holdings. New cross-chain collections need both chain snapshots at the same
requested close; mismatched selected parent or child blocks are unavailable.

Powerglove must classify `/api/tvl/graph` in its Yearn Data lane and proxy it to
the Yearn Data target. The old explicit graph proxy otherwise overrides the
broader `/api/tvl` proxy. Standalone graphs may use the current selection; a graph
paired with a selected TVL view should pass that view's `datasetId`.

## October 9 private Powerglove rollout

The protected hosted API was deployed from commit `0d904de`. The private
Powerglove build routes the graph to that API and displays explicit
`deductedTvlUsd` amounts, including zero-deduction curation flows.

Repair run `488` stores the 27 reviewed positions at their original October 8
blocks. It does not replace historical snapshots or use current RPC balances for
past dates. Dataset
`c09c5663cb62c7178b5d34764e764bcbb537e0ea98ecbf5b3adafece4e0e472c`
contains 1,153,961 chart rows spanning 2,265 closes. An exhaustive comparison
found 17 changed chart rows, all at `1791503999`; every earlier chart row remains
identical. Broader historical router/wrapper recollection remains separate work.

A fresh recovery dump and local rollout receipts are retained under ignored
`data/tvl-overlap-recovery-20261009.dump` and
`artifacts/tvl-overlap-release/`. The latter includes the guarded repair,
publication and verification scripts, input hashes, before/after totals, and
historical identity checks. Credentials are not included in these receipts.

Validation: 588 backend tests passed (23 skipped), 332 Powerglove tests passed,
and the Powerglove production build and changed-file Biome checks passed. Eight
API route checks and desktop/mobile browser checks confirmed the selected graph,
including the $928,357.17 cross-chain deduction. The private proxy protection
credential is absent from the browser build.
