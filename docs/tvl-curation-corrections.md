# Curation scope and nested TVL corrections

Four Turtle Club Katana predeposit contracts were classified as curation by the
old explicit extra-product list. They are Yearn V3.0.4 contracts. Their inclusion
was a reporting mistake, and the approved policy excludes them from **all
historical TVL**, not merely from dates after migration.

`inventories/tvl-vault-policy.json` records their V3 classification and exclusion.
Discovery corrects retained catalogue records even when the upstream omits these
retired vaults; generic enrichment cannot restore the old category. Publication
normalizes the catalogue without writing to the source database. Accounting
removes these vaults and their incoming/outgoing edges before computing totals,
so an excluded parent cannot also deduct a child. Stored balances are preserved.
This is an explicit reporting exclusion, not a claim about a migration date.

Curation V2 → V1 holdings now reduce curation TVL. Vaults → Curation holdings
still retain the previously approved separate-product-layer headline policy.
Missing prices remain unavailable and make internal overlap accounting incomplete.

New manifests pin `vaultPolicies` and `deductInternalCuration`. Their presence
changes the dataset hash, requiring new prepared history and matching analytics.
Existing manifests lacking these fields retain their previous scope and accounting,
including when rebuilding their chart cache. The raw summary fields (`curationTvl`,
`tvlByCategory`) retain their existing gross semantics; external history, graph,
comparison and total TVL use deductions.

## Additional V2 holdings

The existing 19 mapped V2 routes (five original plus 14 additions) remain. Three
more positive-debt holdings were omitted because `delegatedAssets()` returns zero:

| Parent | Strategy | Destination | Additional deduction at October 8 close |
| --- | --- | --- | ---: |
| DAI `0x19d3364a399d251e894ac732651be8b0e4e85001` | `0x3d6532c589a11117a4494d9725bb8518c731f1be` | DAI `0xda816459f1ab5631232fe5e97a05bbbb94970c95` | $409,721.71 |
| USDC `0x5f18c75abdae578b483e5f43f12a39cf75b973a9` | `0x36822d0b11f4594380181ce6e76bd4986d46c389` | USDC `0xa354f35829ae975e850e23e9615b11da1b3dc4de` | $1,482,420.92 |
| Curve stETH `0xdcd90c7f6324cfa40d7169ef80b12031770b4325` | `0x9084b5a98e3b4b257affd82ae4a1753f87906dce` | Curve stETH `0x5b8c556b8b2a78696f0b9b830b3d67623122e270` | $61,067.41 |

At Ethereum block 26151122, `vault()`, `yVault()`, matching underlying tokens,
positive recorded debt and share ownership were verified through archive RPC.
Collection rechecks `yVault()` at each requested historical block. The deduction
remains capped by debt, ownership and parent/child TVL; it does not deduct the
entire strategy wallet. The destination DAI and USDC vaults themselves invest in
V3; both hops remain represented rather than replacing one with the other.

## Review results and remaining dependencies

Using the stored close `1791503999`, the local replay gives known external values:

| Category | Before | After |
| --- | ---: | ---: |
| Curation | $75,569,547.55 | $51,976,346.23 |
| V2 | $73,111,571.00 | $71,158,360.95 |
| V3 | $79,354,852.78 | $79,354,852.78 |
| V1 | $2,381,105.91 | $2,381,105.91 |

The curation reduction is $18,519,273.12 of excluded predeposits plus
$5,073,928.20 of internal holdings. Missing prices still make these known
subtotals, not certified complete totals. The existing DUSD router remains
mapped but unpriced. These figures are a single-close review, not a full
historical data rollout or deployment.

The curation factory inventory now includes Katana V2 and HyperEVM V1/V2.
HyperEVM OUSD V2 `0xe90959cbe7e56b5ebff9ad12de611a4976f2d2b1` was absent from our
inventory. It held 28.230441 USDC through its V1 adapter at block 48034850.
Discovery can now recover that candidate; a production discovery/collection run
is still required. Factory-wide HyperEVM V1 coverage was not certified because
archive RPC log-range limits interrupted the independent scan.

[yearn-prices #63](https://github.com/yearn/yearn-prices/issues/63) tracks chain 999
namespace support and historical USDC/USD₮0 coverage. The OUSD vault's underlying
asset is USDC (`0xb88339cb7199b77e23db6e890353e22632ba630f`), not the OUSD token.
Do not guess a namespace or insert an assumed $1 quote. After the producer defines
and serves its canonical namespace, update `pricing.yearn_prices_token_key`,
recollect historical prices/positions, and regenerate publications.

The $6,167,004.96 Yearn USDC vault remains in our scope. Its initial owner misses
DefiLlama's discovery allowlist. [The prepared upstream patch](upstream/defillama-yearn-usdc.patch)
adds this address explicitly via the existing `morpho` list, without adding the
original owner's unrelated vaults. It has not been submitted or merged upstream.

## Reproduce the isolated review

```bash
PYTHONPATH=src .venv/bin/python scripts/review_curation_corrections.py \
  --frame artifacts/curation-discrepancy-20261012/frame.json \
  --positions artifacts/curation-corrections/additional-v2-verification.json \
  --hyper artifacts/curation-corrections/missing-hyper-collection.json \
  --out artifacts/curation-corrections/new-review
```

The script checks dates/blocks, creates a separate SQLite publication, and writes
input hashes, before/after totals, graph and summary responses. It refuses to
replace an existing review database and never changes the active publication.
For production, apply catalogue corrections, collect the three new V2 positions
and newly discovered Morpho vaults at requested closes, publish/prepare a new TVL
dataset and matching analytics, then verify and activate that release. Historic
V2 deductions require dated recollection; today's balances cannot fill old dates.

## October 12 Neon preparation

Repair run **489** is committed to Neon. It adds 5,165 dated V2 holdings and
221 HyperEVM vault snapshots with 221 corresponding adapter positions. Six V2
positions lack historical prices and remain unavailable. Catalogue classification
is corrected for all four Turtle predeposit contracts. No prior raw observations
were deleted. Recovery before-images and input hashes are retained under ignored
`artifacts/curation-neon-release/`.

The corrected publication has **1,152,206 rows across 2,265 closes**. A full outer
comparison against the previous publication found **8,209 changed rows
across 1,886 dates**, all confined to approved exclusions, internal
curation deductions, the three new V2 targets and the newly inventoried HyperEVM
vault. Existing non-excluded raw chart values are unchanged.

Prepared TVL dataset:
`9f5d1262367eb80c55793f6041c7973d9ecdee383b0d5ae7fb665c48e1de1f57`

Matching analytics:
`a2eea3d82611b11d5dd05d3da1292abc95e9abf11775bf95046a0c9a43138f88`

Prepared hosted release:
`b04af26bf93ac8065d439063d2c788abb9274f3728b80d2ba0b6faba8c32c113`

**This release is staged, not selected.** The live API was verified to remain on
release `55038855420476cccdfb745d808cb966ef7859c59c90f3d489c9d0c0a23706ae`.
Production API deployment was blocked by automatic approval review because the
authorized step covered commit/push, PR creation and Neon updates. Deployment and
activation require approval. Deploy compatible API code before selecting the new
release, then update/redeploy Powerglove PR 50 if its API target URL changes. Do
not run canonical consolidation against the old active release while retaining
this staged publication for rollout.
