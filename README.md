# yearn-data

Reusable Python tooling for indexing Yearn vault data and running research jobs on top of normalized onchain data.

The first analysis job is `lifetime-yield`, which backfills Yearn V2/V3 `StrategyReported` events, prices report-time vault asset gains/losses, and exports aggregate yield totals.

For pricing policy, configuration and Yearn-only commands, see [historical earnings pricing](docs/earnings-pricing.md).

## Quick Start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'

yearn-data init-db
yearn-data discover
yearn-data index-events
yearn-data price --source yearn-prices --no-provider-fallback --no-onchain-fallbacks
yearn-data analyze lifetime-yield --price-source yearn-prices --no-provider-fallback
yearn-data export lifetime-yield
```

By default the CLI loads RPC and API values from `.env` in this repo. Required RPC variables depend on the chains you run:

```bash
ETH_RPC_URL=
POLYGON_RPC_URL=
BASE_RPC_URL=
ARB_RPC_URL=
KAT_RPC_URL=
OP_RPC_URL=
FTM_RPC_URL=
YEARN_ENVIO_GRAPHQL_URL=
YEARN_PRICE_PROD_KEY=
ETHERSCAN_API_KEY=
```

`ETHERSCAN_API_KEY` is optional for most indexing runs, but useful for V2 source/ABI review workflows.

## Useful Options

```bash
yearn-data --db data/yearn.sqlite discover --chains eth arb kat
yearn-data index-events --to-block 25400000 --chunk-size 5000
yearn-data run lifetime-yield
yearn-data run vault-volume
```

The SQLite database stores raw event rows, normalized strategy reports, prices, resumable cursors, and analysis outputs so later research jobs can reuse the same indexed data.

## Lifetime Yield Outputs

The `lifetime-yield` aggregate columns `gross_gain_usd`, `loss_usd`, and `net_yield_usd` are economic totals adjusted for known Yearn public incident disclosures where vault reports emitted paper losses or compensating phantom profits. The raw report-time accounting values remain available as `raw_gross_gain_usd`, `raw_loss_usd`, and `raw_net_yield_usd`.

Rows changed by an incident adjustment are marked in `reports.csv` with `is_adjusted`, `incident_id`, `incident_classification`, `incident_description`, and `incident_disclosure_url`. Raw indexed `strategy_reports` rows in SQLite are not modified.

### Redis Headline

After a completed `lifetime-yield` analysis run, publish the odometer headline payload with:

```bash
REDIS_URL='rediss://...' yearn-data publish lifetime-yield --ttl 259200
```

The publisher atomically replaces `lifetime_yield:headline` with one JSON blob containing `net_yield_usd`, `as_of_ms`, `rate_usd_per_sec`, previous anchor fields, and `run_id`. Use `--dry-run` to print the payload without touching Redis.

## Vault Volume Outputs

The `vault-volume` job indexes user `Deposit`/`Withdraw` events and strategy debt movement. V2 strategy allocation volume is derived from `StrategyReported.debtAdded` and `debtPaid`; V3 allocation volume is indexed from `DebtUpdated` events.

The headline volume metric is `gross_total_volume_usd`, defined as deposits + withdrawals + allocations + deallocations. Exports also include net user flow, net strategy flow, per-chain/vault/strategy/token/month rollups, and raw `vault_flows.csv` / `strategy_debt_flows.csv` event files.

## Pricing

Earnings default to Yearn Prices with optional DefiLlama fallback. For the
Yearn-only delivery workflow, use the explicit flags in the quick start. Volume
pricing retains its DefiLlama path:

```bash
yearn-data price --source defillama
yearn-data price-volume --source defillama
yearn-data price --source defillama --no-onchain-fallbacks
```

When DefiLlama is enabled and cannot price a token directly, its pricing path can apply local deterministic fallbacks for canonical stablecoins, canonical wrapped/native equivalents, selected Curve/CRV derivative tokens, exchange-rate wrappers, and Aave aTokens. Use `--no-onchain-fallbacks` to record only direct DefiLlama results. All source/status rows are stored in SQLite.

## Backfill Efficiency

Event indexing uses resumable `eth_getLogs` block chunks and dedupes logs by `(chain_id, tx_hash, log_index)`. The default chunk size is `50,000` blocks, intended for Tenderly-style archive RPCs; lower it if an RPC returns block range errors. Block timestamps are cached in the local database after first lookup.

## Historical report ingestion

See [the Envio ingestion guide](docs/historical-envio-ingestion.md) for retired vault discovery, metadata and bounded replay.

For explicit per-chain RPC/Envio windows, including Optimism and Fantom, see [bounded report catch-up](docs/bounded-report-catchup.md).

For the complete scriptable workflow and reproducible CSV exports, see
[Refresh and export earnings and fees](docs/earnings-and-fees.md).
