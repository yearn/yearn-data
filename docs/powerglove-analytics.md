# Owned Powerglove analytics on Postgres

The active stats consumers can use Yearn Data for TVL, fees, reported earnings,
DefiLlama comparison and membership, nested fee stacks and profitability. The API
reads stored observations and prepared analytics from Neon. It does not query the
legacy services, their databases, Kong, DefiLlama or an RPC while serving requests.

Keep `NEON_DB_URL` in Yearn Data's ignored `.env`. Install this checkout with
`python -m pip install -e .`; when using an environment installed against another
checkout, set `PYTHONPATH=src` for the commands below.

```bash
python -m yearn_data.cli --env .env --db neon collect-analytics
python -m yearn_data.cli --env .env --db neon prepare-analytics \
  --publication data/powerglove-neon --tvl-publication data/powerglove-tvl-neon
python -m yearn_data.cli --env .env --db neon serve-pairing --analytics \
  --publication data/powerglove-neon --tvl-publication data/powerglove-tvl-neon \
  --current-bridge-policy retired-registry --host 127.0.0.1 --port 3493
```

Use the existing imported financial publications; selecting different financial
analyses remains the existing `select-pairing` operation. Do not pass
`--comparison-db` for this cutover. Collection and preparation are explicit worker
operations. Refresh them after updating core financial/TVL inputs; API requests do
not initiate a refresh of these prepared analytics.

Configure Powerglove's local target as
`VITE_YEARN_DATA_API_TARGET=http://127.0.0.1:3493`. A deployed consumer instead needs
`VITE_PUBLIC_YEARN_DATA_API_URL` pointing to the public read-only API. Never expose
the database connection as a Vite variable. The hosted database does not itself
host the HTTP API.

## Stored data and selection

`analytics_inputs` retains immutable, dated reference, membership and fee
configuration observations. `analytics_publications` retains the prepared
responses, contributing IDs, cutoffs and methodology. `analytics_selection`
atomically selects the current publication. The same Postgres store also holds the
imported TVL, fees, reports and prepared daily TVL chart rows.

Known zero rates/amounts are preserved. An unavailable retry retains a previous
known observation and records the retained observation's age; it does not erase
known inputs. Source coverage remains partial when collection fails. A preparation
failure leaves the previous selected publication usable.

`GET /api/analytics/publication` returns the selected metadata. The three new
routes and `/api/comparison` accept `publicationId`; this pins their immutable
result. Powerglove selects once per API origin and pins subsequent analytics
requests. The selected analytics records its own `tvlDatasetId` and
`feesDatasetId`, independently of a newer core publication. All clients can inspect
these source IDs and observation times. Reopen the page to select a newer
analytics publication.

## Methodology

- DefiLlama totals: collect `yearn-finance` and `yearn-curating` directly from
  `api.llama.fi`, preserving provider timestamps and payload fingerprints. They are
  external reference figures, not a second source of local Yearn accounting.
- Membership: evaluate the local catalogue against the frozen finance adapter's
  Kong candidates, exclusions, positive on-chain assets, V1 rules and nested debt
  deductions. Curation uses the last retained Yearn curation adapter configuration,
  its explicit ERC-4626 vaults and factory creation-owner evidence. The frozen
  source URLs/text/hashes and per-vault blocks/evidence are stored with the input.
  This is an adapter reconstruction, not a DefiLlama-supplied vault balance feed.
  Unknown queue/debt reads or missing creation evidence cannot prove inclusion or
  absence. Unsupported finance chain-scope changes fail collection rather than
  silently extending the reconstruction.
- Fee configuration: retain Kong's declared fee metadata as diagnostic evidence;
  validate supported current rates through on-chain V2/Tokenized Strategy getters,
  standard Accountant `getVaultConfig(vault)` and Morpho V1/V2 getters. Morpho WAD
  rates become bps. Unsupported or failed configuration reads remain unavailable;
  no legacy default performance-fee estimate is applied.
- Fee stacks: use the current native dated positions, including intermediary
  targets. Cap attributed path capital, bound depth at ten and detect cycles.
  Performance rates compose successive fixed-rate charges with
  `1 - product(1 - rate)` and capital weighting. Annual management rates are summed
  along each path under a common annual/capital basis, then capital-weighted.
  These are configured-rate illustrations: Accountant caps, refunds and differing
  realization times can change actual charged fees. Unsupported or unresolved
  paths have null effective rates. External root fractions determine the stack
  capital summary; nested roots do not each contribute their full gross capital.
- Profitability: use the trailing 365 UTC days ending at the earlier supported
  financial/TVL cutoff, with equal-duration daily capital observations. The
  denominator is mean observed daily gross contract TVL, not the latest balance.
  Do not fill gaps or prehistory with zero. Eligibility requires at least 90
  observed days and 80% coverage from the first supported observation; fees before
  supported TVL history make annualized yield unavailable. Complete priced fee
  observations are required for a fee-yield ratio. Annualization uses the supported
  elapsed duration, and both trend halves use the same rules.
- Trends: adjacent half-year periods; at least three reports; changes above/below
  0.005 are improving/declining, otherwise stable. Unsupported comparisons are
  `insufficient_data`. Pricing confidence is high at 80% priced fee coverage,
  medium at 40%, otherwise low; it is coverage, not a guarantee of quote accuracy.
  Quadrants use the displayed cohort ($10k–$100M current gross TVL) within the
  requested chain scope, with current TVL on the capital axis and time-weighted
  fee yield on the yield axis. High yield is strictly above the cohort median.
  Explicitly observed zero fees remain low yield; missing fee observations are
  unavailable and excluded from classification.
- Fees charged at different layers remain distinct. Earnings are incident-adjusted
  contract-level reported gains, not consolidated nested economic yield. Summary
  profitability denominators are gross contract capital and are labeled as such;
  headline external TVL retains its existing native deductions.

## Routes and developer checks

| Route | Parameters |
| --- | --- |
| `/api/comparison/defillama-comparable` | `publicationId`, `includeVaultBreakdown=false` (missing list retained) |
| `/api/fees/stack` | `publicationId` |
| `/api/profitability` | `publicationId`, optional `chainId` |
| `/api/comparison` | `publicationId`, optional matching `datasetId` |

Developer coverage is in response availability fields and publication input
metadata. Financial nulls render as unavailable amounts; the UI does not add a
quality dashboard. Only verified members show a DL indicator. The unused legacy
route fallbacks remain configured, but no active migrated stats request needs them.

Validate the three new sections, aggregate comparison and existing core charts
through the actual Powerglove proxy. Check the selected IDs, browser errors and
request routing. The API must start without a legacy comparison database and
return the prepared values with legacy services inaccessible. Retain prior
publications for rollback; no production deployment is implied by a local preview.

## Local cutover validation

The selected publication `574c64f1944806a262e1f69fa454fb3a1574fa870b8c1f266792d1e585aadeff`
contains 45 nested stacks (33 with supported effective rates), 1,023 profitability
records (547 eligible for fee-yield calculations), and 510 confirmed included / 15
confirmed missing / four unknown currently counted vault membership records.
Source gaps remain developer work; they do not require either legacy service.

The four unknown membership records are Ethereum vaults
`0x71955515adf20cbdc699b8bc556fc7fd726b31b0`,
`0xb98df7163e61bf053564bde010985f67279bbcec`,
`0xcb550a6d4c8e3517a939bc79d0c7093eb7cf56b5`, and
`0xe11ba472f74869176652c35d30db89854b5ae84d`. Their queue/debt reads are unavailable
under the supported finance-adapter ABI. The other unavailable stack rates are
identified in stored configuration and allocation coverage. Do not replace them
with zero or infer DefiLlama inclusion from their absence in the missing list.

Validation passed: 508 backend tests, 15 Postgres transport/integration tests using
disposable Neon schemas, eight affected frontend tests, TypeScript checks,
production build and browser checks of the three new sections and comparison.
The new API was started with `--analytics` and no `--comparison-db` argument. A separate browser check also passed with both legacy proxy targets deliberately
set to an unreachable port. The preview proxy uses this API for all actively
consumed TVL/fees stats routes.
Browser receipts and screenshots are retained locally under
`artifacts/owned-analytics/`; no private preview addresses belong in public PRs.


## Quadrant and monthly denominator correction

The earlier classifier treated missing fee observations as zero and calculated
thresholds across retired and tiny vaults that were absent from the scatter plot.
This could produce a zero median and classify every observed yield as high. The
current methodology excludes unavailable observations, keeps genuine zero fees
low, and uses the displayed capital cohort for the thresholds.

Exact additional quote rejections are frozen in
`inventories/tvl-price-rejections.json`: 19 Ethereum Curve-related quotes at the
December 20, 2023 UTC close and one yETH LP quote at the January 29, 2026 close.
Historical supply, reserves, virtual prices and adjacent observations are retained
as evidence. No later price or fabricated zero replaces them. These are additional
asset-days beyond the yBUSD observations in yearn/yearn-prices issue #61; canonical
upstream quote repair remains necessary.

The repaired publication recalculates accounting and nesting deductions for those
dates and reuses unaffected immutable chart rows. December 2023's average known
external TVL is about $329M, December 2024's is about $268M (no comparable outlier),
and January 2026's is about $562M. The publication preserves missing asset values
as unavailable while serving the other known observations.
