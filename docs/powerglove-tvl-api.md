# Powerglove TVL cutover

The read-only pairing server can serve TVL alongside fees and reported earnings from
`data/yearn.sqlite`. Powerglove's `codex/stats-page` uses this source for current TVL,
history, constant-price history, curation products, the vault audit tree and the
Yearn side of the DefiLlama comparison. Fee-stack and profitability analytics retain
the existing fees service. Supplementary graph endpoints retain the existing TVL
service until those consumers migrate.

## Start the API

Keep the existing fees/earnings publication selected, then run:

```bash
yearn-data serve-pairing --publication data/powerglove \
  --tvl-publication data/powerglove-tvl \
  --current-bridge-policy retired-registry \
  --comparison-db /path/to/defillama-reference.sqlite \
  --host 127.0.0.1 --port 3492
```

The optional comparison database supplies only stored `defillama_snapshots`
reference rows. No Yearn TVL figures are read from it. The API makes no RPC or
pricing requests and opens its source databases read-only. Configure a reverse
proxy for production; set `--cors-origin` when Powerglove uses a different origin.

Powerglove configuration:

```text
VITE_YEARN_DATA_API_TARGET=http://127.0.0.1:3492
# Production, when the API is on a separate browser-accessible origin:
VITE_PUBLIC_YEARN_DATA_API_URL=https://your-data-api.example
```

Existing TVL and fee service settings continue to serve supplementary analytics.
Without a Yearn Data target, the TVL proxy retains its existing TVL-service fallback.

## Publication and automatic selection

The first startup publishes accumulated finished TVL runs. Subsequent unpinned
requests check for new finished observations at most every 30 seconds. There is
no manual collection-run selection. A newer old-date backfill repairs that date
without replacing recent observations or discarding other history. Known valuations
win over unavailable retries; successful later observations win ties.

The current close is the latest stored date with observed quantities on every
collected chain. A whole-chain RPC failure leaves the preceding common close
selected until a repair supplies that chain. Pricing gaps and unresolved positions
remain in developer diagnostics. Verified invalid provider quotes are rejected by exact
chain/asset/day/price matches in `inventories/tvl-price-rejections.json`, frozen into
the publication. Historical reserve evidence is retained there. Rejected values
remain unavailable until a corrected canonical quote is collected; no fallback
USD price is substituted. Reference candidates are filtered before selecting the
first seven valid daily observations, so rejected quotes cannot exhaust that
window or set its constant price. HTTP responses serve available stored amounts;
unavailable amounts remain null rather than fabricated zero.

`datasets/<dataset-id>.json` freezes finished source-run membership, the vault
catalogue, positively identified V3 allocators, selection policies and current-close
diagnostics. `current.json` activates it atomically after validation. Failed
publication retains the preceding selection. Completed source observations must
remain immutable and available while clients use their dataset IDs.

The TVL summary returns `datasetId` and `asOfTimestamp`. Powerglove pins its history,
constant-price chart and chain drilldown to this identity. A stored dataset remains
addressable after a subsequent collection publishes another one.

## Routes and accounting

| Route | Stored result |
| --- | --- |
| `/api/tvl` | Current summary, chains, categories, retired vaults and deductions |
| `/api/tvl/history/runs/latest` | Sampled historical TVL |
| `/api/tvl/history/runs/latest/constant-price` | Actual and constant-price history |
| `/api/tvl/curation-products` | Morpho and positively identified V3 allocator products |
| `/api/audit/tree` | Dated vault/strategy positions and current bridge exclusions |
| `/api/comparison` | Native Yearn figures versus stored DefiLlama references |

Filters include `datasetId`, `chainId`, inclusive `from`/`to`, `groupBy`
(`chain`, `category`, `type`, `vault`), `mode` (`external`, `raw`) and `interval`
(`daily`, `3day`, `weekly`). History retains the last observed stock in each bucket;
it does not sum daily TVL. Empty dates and artificial zero baselines are not added.
The legacy `includeCurrent` parameter is accepted; current means the latest stored
close, never a live RPC snapshot.

Native accounting caps verified nested holdings by both parent and child TVL.
The headline retains the existing service's separate curation-layer convention;
the curation-product view explicitly deducts verified curated child ownership.
Powerglove uses external TVL directly rather than scaling raw historical values to
match today's headline. Constant-price history freezes each vault's asset-unit
price at a reference inside the requested window, using the existing first-seven
observation outlier rule. It applies the dated external fraction to those values.
It measures asset-unit change at frozen prices, not independently reconstructed
net deposit flows; share-token assets retain their stored share-unit definition.

The four Ethereum Katana pre-deposit vaults are excluded from external TVL from
June 30, 2025 at 13:06:47 UTC (09:06:47 Eastern), inclusive. Before that cutoff,
their TVL remains included even though today's catalogue marks them inactive.
Raw history retains the stored balances throughout. Both actual and constant-price
history apply the same dated external fraction.

The registry stores the migration timestamp, Ethereum block 22817424, destination
network ID 20 and transaction
`0x6896487aac1fe132614719e7758719d55d11036bb163d1a959ed9500d8cd30f0`.
This transaction set all four deposit limits to zero and bridged their USDC, USDT,
WBTC and ETH positions to Katana. The publication freezes this evidence under
`bridgeMigrations`; older dataset IDs retain their original accounting context.
Excluded parent positions remain in diagnostics but no longer deduct child TVL.

Undated bridge registry entries retain the optional latest-close retirement
fallback. It is never projected backward into history. Verified migration dates
take precedence over that fallback and over today's active flag. Fee charges at different layers
remain separate charges; reported earnings remain contract-level P&L rather than
consolidated nested economic yield.

## Collection operations

Collect or repair source data through the existing TVL commands, then allow the
server's next refresh to publish it. Check finalized head freshness on the actual
RPC before interpreting a failed close as a chain-level finalization problem.
Katana's configured dRPC endpoint returned a stale finalized head during cutover;
healthy public endpoints supplied the requested historical close and the Katana-only
repair completed. RPC endpoint configuration is local and contains no committed
credentials. Refresh the existing fee/earnings analyses separately when their
source data changes; TVL collection does not regenerate those analyses.

## Historical query performance

Chain, version and constant-price charts reuse compact, dated valuation rows
within a dataset. The shared row cache is bounded to four windows of at most 400
observed dates; longer histories keep streaming. Reference prices are cached
separately for eight daily windows, independent of chart grouping. Explicit
all-time first/last bounds share the same response cache as omitted bounds.
Concurrent requests for identical source work join that calculation, while
current summaries and unrelated reads remain available. The reference-price SQL
sorts observation identities rather than full snapshot payloads.

A first uncached history or reference window still performs a stored-data scan;
subsequent group changes reuse those observations. These caches are in memory
and are rebuilt after restart or a new dataset selection. No historical valuation,
reference-price rule or reporting cutoff changes as a result of caching.
