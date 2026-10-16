# Bidirectional TVL comparison

The category drill-down compares independently checked adapter scope, not whether
an address has a positive *net contribution*. Yearn Data deducts ownership from
children; DefiLlama Finance deducts nested debt from parents. A fully invested
parent can therefore contribute zero there without being excluded from scope.

Prepared `comparison-evidence` inputs are pinned to a TVL dataset and close.
`analytics.prepare` includes their content ID in the publication identity and
exposes the result as `reconciliation` on `/api/comparison/defillama-comparable`.
Evidence from another dataset returns unavailable. Prior publications remain
unchanged. These inputs replace older inventories rather than accumulating them.

The drill-down separates:

- Our valued vaults outside their independently checked scope.
- External valued contributions absent from our valued data. Missing local prices
  remain null, not zero; independently absent local observations are separate.
- Positive ownership deductions whose route their adapter does not recognize.
- Routes already handled by both, and unresolved checks.

Reverse contributions use historical DefiLlama token quotes with confidence at
least 0.5 and timestamps within one hour of the close. They are labeled adapter
replay values, not published per-vault DefiLlama TVL. Known nested curator shares
are deducted and the pinned upstream bad-debt helper is checked first. Overlap
amounts use our capped dated deductions, not an asserted dollar-for-dollar
reconciliation of published protocol totals. Different dates, prices, rounding,
attribution and unpriced assets can leave a residual.

## October 8 close reviewed October 12

At dataset `9f5d1262367eb80c55793f6041c7973d9ecdee383b0d5ae7fb665c48e1de1f57`,
using adapter revision `e5c4535df5ce9193b5f7f350943c4018553a4c23`:

- 22 intermediary/router routes: $30,190,158.28 deducted here; the upstream
  Finance registry contains only the two DAI-1 routes, which are handled separately.
- Five vault-share asset wrappers: $11,305,900.09 deducted here, including staked
  yBOLD and locked yvUSD. The Finance loop does not deduct ownership of its vault
  share assets.
- One cross-chain ownership route: $928,357.17 deducted here (USD yVault to Katana
  vbUSDC). The adapter's nested-debt detection is same-chain.
- Total deductions on those 28 routes: $42,424,415.54. This is a method difference
  at our close, not a claim that every corresponding asset receives a price in
  DefiLlama's published total.
- OUSD V2/V1 nesting is already handled by the curator adapter. Our comparable
  capped deductions total $5,073,928.20; these are not missed DefiLlama overlaps.
- Reverse-price differences include Yearn KAT (about $300,918), Yearn OG WETH on
  Arbitrum (about $854), and OUSD V1/V2 on HyperEVM (about $733 combined after
  removing their internal ownership). Their local prices remain unavailable.

All 929 independently fetched Finance Kong candidates are present in our frame.
Curator discovery covers configured factories and supplements them with verified
creation-owner evidence. The HyperEVM V1 factory scan remains incomplete. The
report does not claim exhaustive coverage there, or confirmed USD differences for
195 Finance vaults whose external prices remain unverified.

## Reproduce and publish

`PYTHONPATH=src python scripts/review_tvl_comparison.py ARTIFACT_DIRECTORY`

The directory is a captured evidence bundle with:

- `frame.json`: selected publication identity, timestamp, raw/accounted rows,
  dated positions and catalogue, read from the hosted TVL dataset.
- `kong.json`: full per-chain `origin=yearn` candidate responses, excluding the
  adapter blacklist. Explicit V1 addresses are added separately.
- The pinned Finance, curator registry, curator helper and factory source files
  listed in `sources.json`, plus `morpho-bad-debt.cjs`.
- `independent-discovery.json`, `verified-membership.json`: archive factory scans
  and individual creation-event evidence at the selected close.
- `adapter-replay*.json`: archive liquidity-adapter/share conversion calls.
- `reverse-bad-debt.json`: outputs from executing the pinned upstream bad-debt
  helper at those archive blocks. The reviewer refuses missing/changed haircuts
  until a matching reconciliation has been supplied.
- `prices.json`: cached historical coin responses; if absent, the reviewer obtains
  them from DefiLlama. No external writes occur.

The script checks known source hashes and writes `evidence.json`, including hashes
of its inputs. It fails if independent discovery finds a vault without a matching
archive observation; collect that observation before proceeding. New adapter
revisions require review rather than silently using older recognition rules.

To release, insert the reviewed body using
`put_input(conn, 'comparison-evidence', body, 'partial')`, prepare matching analytics,
and select a coherent hosted release through the existing publication workflow.
Refresh the evidence for a new TVL dataset; do not retag the old bundle. No price
repairs or exclusions are made by this comparison workflow.

The current private preview uses a separate read-only response server over the
captured bundle. Production analytics and Neon selection have not been changed.

## Refresh after the Finance and curation adapter updates

The current collector reads the Yearn entry in `registries/curators.js` rather
than the retired standalone curation adapter. Isolate that entry before parsing
chain configuration: other curators use the same chain names.

For a new close, capture all upstream files at one commit, then run:

```bash
PYTHONPATH=src python scripts/collect_finance_comparison.py ARTIFACT_DIRECTORY \
  --timestamp UTC_CLOSE_TIMESTAMP
```

This saves current Kong candidates and dated Finance contributions, including
registered route destination checks, ownership caps, converter decimals, wrapper
exclusions, and the verified remote allocation. Missing reads remain unavailable.
Factory discovery, curation adapter/share calls, bad-debt checks and price quotes
must also be refreshed at the same close before running `review_tvl_comparison.py`.
The reviewer accepts only reviewed source hashes, and the new Finance source
requires its dated `finance-replay.json`; it cannot reuse the old recognition-only
replay. Retained factory events can support discovery only with continuous scan
coverage and exactly matching factory ABI and owner filters.
