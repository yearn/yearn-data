# Historical TVL and nested positions

The pipeline stores a dated asset quantity for every vault in the selected inventory,
prices it, and removes known nested ownership when combining vaults. It includes
retired vaults. Collection is separate from fee and transaction-event ingestion.

For example, a parent with $100 invested in a child with $100 has $200 of gross
vault TVL but $100 of combined TVL. A third nested layer is deducted again at that
layer. The child retains any capital supplied by unrelated depositors.

## Run it

Start from an empty database with independent discovery. The command combines
Kong V2/V3 inventory, the bundled 28-address Yearn V1 registry, paginated Morpho
owner/creator/curator queries, factory creation events, and explicitly listed
curated products. It requires no old TVL service checkout or database.

```bash
yearn-data --db data/tvl.sqlite --env .env tvl discover

# One daily UTC-close sample: 2026-10-03 23:59:59 UTC.
yearn-data --db data/tvl.sqlite --env .env tvl collect \
  --from-timestamp 1791071999 --to-timestamp 1791071999

yearn-data --db data/tvl.sqlite tvl export --out exports/tvl
```

Discovery supports `--sources kong v1 curation` and `--chain-ids 1 8453`.
Omitting those filters selects all sources and their configured scopes. Factory
and historical adapter scans resume from successful stored coverage. Their first
run can take longer. `--from-block`, `--to-block`, and `--chunk-size` bound those
scans; API identities and metadata still describe current state. Use a chain
filter when setting block bounds, because block numbers differ across chains.

The final JSON summary reports each selected source, metadata failures, mapping
failures, and scan scope. Complete discovery exits 0; incomplete discovery exits
2 and retains candidates so the same command can be retried. Discovery summaries
are also stored in `tvl_discovery_runs`. A failed source does not prevent the
other selected sources from completing. Discovery does not call a price provider.

`YEARN_MORPHO_GRAPHQL_URL` optionally overrides the public Morpho API endpoint.
RPCs must provide the selected historical logs; enriching old adapter contracts
can also require archive state. Missing access is reported as incomplete.

Migration remains optional. It brings across an existing catalog and relationships,
without importing current balances as historical values:

```bash
yearn-data --db data/tvl.sqlite tvl import-catalog \
  --service-db ../yearn-tvl-service/packages/db/yearn-tvl.db
```

Without filters, collection requests **every vault in the database**, across all
its chains and categories. Use `--chain-ids 1 8453`, `--vaults <addresses>`, or a
shorter date range for bounded work. Daily intervals are the default; use
`--interval 604800` for weekly history. Yearn Prices requires UTC-close timestamps
and whole-day intervals so quantities and prices represent the same date.
`--price-source defillama` allows explicit timestamps and uses the existing
DefiLlama historical-price client.

Set `YEARN_PRICE_PROD_KEY` for Yearn Prices. TVL RPC selection uses, in order,
`ARCHIVE_RPC_URI_FOR_<chainId>`, `ARCHIVE_RPC_URL_<chainId>`,
`RPC_URI_FOR_<chainId>`, and the existing chain's named RPC variable. This also
supports inventory chains outside the fee indexer's default chain list.
Each chain is sampled at its last block at or before the requested timestamp,
within the pinned finalized head. Ethereum-style RPCs use the `finalized` tag;
Fantom Opera uses its published main-chain head, whose blocks already have
[consensus finality](https://docs.fantom.foundation/technology/faq). Both policies
pin a single block before historical lookups. Missing archive access remains unavailable.

## Map additional nested positions

Direct allocator-to-tokenized-strategy relations are identified by address.
The migrated registry covers known intermediary strategies and retired V2 routers.
Vaults whose underlying asset is another vault's share token are detected automatically.

To find additional intermediaries, scan their child share balances at an explicit
date. This scans all known strategy and vault holders against the chain's vaults,
so run it deliberately rather than on every daily collection:

```bash
yearn-data --db data/tvl.sqlite --env .env tvl map-holders \
  --chain-ids 1 --timestamp 1791071999
```

Repeat at older dates to find historical routes that no longer hold shares today.
Locally indexed strategy reports, debt flows, and strategy-change events also add
historical candidates. Rerun `tvl discover` to refresh V1/curated inventory and discover adapter paths.
Specific Morpho families are retained separately from product category. All
registered adapters are checked, and previously added adapters remain candidates
through their `AddAdapter` events. Unknown child mappings are retained with
unresolved diagnostics. Positive debt in an unresolved adapter makes a collected
aggregate incomplete; a verified zero debt does not require an overlap deduction.
Children outside the configured Yearn catalog remain visible as relations, without
adding their full TVL to Yearn's inventory.

## How deductions work

- **Allocator → tokenized strategy:** use the allocator's recorded debt, capped
  by the strategy's dated TVL. Allocators need not hold tokenized strategy shares.
- **Intermediary → child vault:** measure `balanceOf(holder) / totalSupply` at
  that block, multiply by child TVL, and cap by the parent's recorded debt.
- **Compounder → child vault:** retain this as a second nesting level. Use the
  compounder's asset value as its budget and measure its child shares.
- **Vault with a vault-share asset:** value those shares from the dated child,
  including when the price provider lacks a share-token price.
- **Morpho V2 adapters:** verify historical `isAdapter` membership and read
  `realAssets()` before valuing the adapter's child holdings. See the
  [Morpho contract reference](https://docs.morpho.org/developers/contracts/morpho-vaults-v2/)
  and [adapter interface](https://github.com/morpho-org/vault-v2/blob/main/src/interfaces/IAdapter.sol).

V2 and V3 use their different `strategies(address)` layouts. A holder shared by
several parents is counted once and split by parent debt. Several child positions
share the same debt budget. Incoming deductions cannot exceed child TVL, and
outgoing deductions cannot exceed parent TVL.

Product category is retained separately from the contract version used for reads.
Curation is kept as a separate product layer by default, matching the old service.
Use `tvl export --include-curation` for a combined view that deducts curated child
ownership too. Every relation remains visible in the position export.

## Stored data and outputs

| Table/output | Contents |
|---|---|
| `tvl_discovery_runs` / `tvl_catalog_evidence` | Source outcomes, metadata provenance, and unresolved mapping diagnostics |
| `tvl_runs` | Explicit date range, selection, pricing policy, acquisition status |
| `tvl_snapshots` / `vaults.csv` | Raw integer assets/supply, normalized quantity, historical block, price provenance, gross and external TVL |
| `tvl_positions` / `positions.csv` | Parent, strategy/holder, child, debt, share ownership, mapping method, deduction |
| `history.csv` | Combined gross TVL, known overlap, external TVL, valuation coverage |
| `tvl.json` | All outputs and run metadata for consumers |

Each collection creates a new immutable run. Recollect an unavailable date after
fixing RPC/pricing access; the earlier run stays available via `--run-id`.
An interrupted collection is marked failed, and default export selects finished
runs. A genuine zero is stored as zero; an unavailable value is null. Incomplete
aggregates have null headline totals and separately named `known_*` subtotals.

Positions describe capital allocated at a point in time. Deposits, withdrawals,
and debt movements remain in the existing `vault-volume` workflow; TVL changes
also include yield, losses, and price changes and are not pure deposit flows.
Raw quantities are retained for later price-neutral analysis.

## Coverage boundaries and validation

A finished run means the selected reads and known candidate positions were
collected; it does **not** prove exhaustive topology or full historical inventory.
Every output identifies the mapping scope as known candidates. A zero balance in
a scan at one date does not disprove earlier nesting. Current API inventory can omit old routes. Discovery preserves previously found
vaults, and factory creation roles and locally indexed events add historical
candidates. Bundled curator identities and factories define the discovery scope;
this is not proof of every historical Yearn affiliation.

The bridge registry identifies migration sources but supplies no effective dates.
When a positive source and its destination chain are both selected, aggregate
headlines are marked incomplete instead of silently excluding all historical
source TVL. No current bridge state is projected into past dates.

Focused tests cover nested layers, shared holders, debt/TVL caps, zeros and missing
evidence, cross-chain identity, V1 quantities, V2/V3 layouts, Morpho membership,
wrapper valuation, migration, and exports. Batched acquisition is tested through
collection and export against ordinary reads, including deployment boundaries,
Multicall fallback, and unavailable values. A built wheel was validated from an
empty database with old-service access blocked. A separate live Ethereum discovery found 637 unique vaults in the selected
scope (600 Kong candidates, 28 V1 candidates, 13 curated candidates, with overlap)
and collected/exported a V1 and curated-vault sample at one historical block.
The live factory smoke window was bounded to 1,001 recent blocks. Earlier checks
also exercised a retired V2 router and allocator/curated-child positions. The
subsequent monthly backfill is described below; service-wide numerical parity
is not required for these logic checks.

## Running the historical backfill

`scripts/backfill_tvl.py` collects daily UTC closes in independent monthly runs,
newest month first. It calls the same `collect_tvl` accounting code as the CLI;
batched RPC requests only change acquisition speed. Deployment boundaries are
verified against archive code, and successful prices and dated blocks are cached
in the selected database. It never imports historical TVL values.

The October 2026 local job uses `data/tvl-backfill.sqlite`, leaving the existing
fee/earnings database untouched. Its catalog comes from independent discovery,
with deployment hints and historical strategy identities from this project's
existing event database. Its finalized-head manifest records the RPC checks.

```bash
PYTHONPATH=src python scripts/backfill_tvl.py \
  --db data/tvl-backfill.sqlite \
  --heads artifacts/tvl-review/backfill/heads.json \
  --env /path/to/.env \
  --out artifacts/tvl-review/backfill \
  --to-date 2026-10-05
```

Restarting the same command reuses saved monthly collections, including those
marked incomplete because of missing data. Interrupted collections are collected
again as a new run; interrupted exports reuse their saved collection. Earlier
runs stay in the database for inspection. The job
writes `status.json` and one export folder per chain/month. A process lock prevents
two backfills from writing to the same database at once.

The current scope starts with the earliest verified vault deployment, no earlier
than January 2020, and ends at October 5, 2026's UTC close. Chains without a usable
finalized archive RPC remain pending. Missing prices or reads remain unknown.
Factory discovery receipts and known candidate mappings define catalog coverage;
this job does not certify that every historical holder or bridge transition has
been discovered. Curation remains separate in the monthly exports. Per-chain
exports do not claim a combined cross-chain headline TVL.

Archive settings also serve ordinary full-node reads. `ARCHIVE_RPC_URI_FOR_<id>`,
`ARCHIVE_RPC_URL_<id>`, and `RPC_URI_FOR_<id>` take precedence over named settings
such as `ETH_RPC_URL`; one archive-capable endpoint is sufficient for both uses.
The local TVL worktree shares the main repository's ignored `.env` via a symlink.
Restart the backfill to load environment changes; saved monthly batches are kept.

Each monthly export gets an `export-complete.json` receipt only after all files
are written. On restart, missing files or a missing/mismatched receipt cause the
export to be regenerated from its saved run, without collecting that batch again.
Older exports without receipts are regenerated once to establish completion.

The October 6 RPC refresh enabled Gnosis, Fantom, Robinhood (4663), and HyperEVM
alongside the existing six chains. Katana discovery uses 50,000-block log ranges
because its RPC rejects larger historical requests. Sonic (146) and Berachain
(80094) are excluded from TVL discovery and collection by project scope. Yearn Prices
namespaces for Gnosis, Sonic, Berachain, and Robinhood are explicitly mapped;
HyperEVM balances can be collected, but its USD prices remain unavailable under
the currently configured Yearn Prices namespaces. These limitations are reported
in the progress preview rather than converted to zero.

Post-run validation found valid zero-padded ABI returns on early Ethereum Yearn
vaults. Scalar getters and strategy tuples now accept a declared ABI prefix with
zero trailing words, and continue to reject short or nonzero trailing data. The
legacy repair retains original runs and re-collects only monthly batches with
failed native reads; missing Yearn Prices quotes remain explicit. Live standard
ABI comparisons and regression checks are retained beside the backfill exports.

Empty V1 vaults are valued at zero under the share-supply TVL method without
calling an undefined price-per-share getter. Residual read retries retain their
original runs and use the same pinned historical blocks.

Sonic and Berachain are excluded even when returned by upstream catalogs or
retained in an existing database. They do not produce pending-RPC notices.
Existing records are retained; the TVL scope excludes them rather than deleting
shared catalog history.
