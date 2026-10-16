# Refresh and export earnings and fees

Use the same database path for every command.
You can put the commands in a shell script.

The workflow has five steps:

1. Collect reports and fee events.
2. Calculate fees in asset units.
3. Fetch prices.
4. Run the analyses.
5. Export each completed run.

To update prices only, start at step 3.
You do not need to collect reports again or repeat verified fee calculations.
Old analysis runs remain available by their run ID.

## 1. Collect the required history

Use `catch-up` for allocator reports:

```bash
yearn-data --db /path/to/yearn.sqlite catch-up --chain CHAIN \
  --from-block START --to-block END --discover
```

Replace `CHAIN`, `START`, and `END` with your selected chain and block limits.
Both limit blocks are included.
Run the command separately for each required chain:

| Key | Chain |
| --- | --- |
| `eth` | Ethereum |
| `op` | Optimism |
| `ftm` | Fantom |
| `polygon` | Polygon |
| `arb` | Arbitrum |
| `base` | Base |
| `kat` | Katana |

The command selects the configured source and retains known retired vaults.
It records completed ranges for those vaults.
This does not prove that every historical contract has been found.

Collect Tokenized Strategy fees separately with `index-tokenized-fees`.
Set its inventory, chain, block limits, cutoff, and maximum vault count.
See [fee accounting](fee-accounting.md).
The packaged inventory is a maintained list, not complete historical discovery.

If the RPC finalized block is stale, you can use `--confirmations 1000`.
This permits blocks up to the latest block minus 1,000 blocks.
Save the printed finality policy with your acquisition records.
The buffer does not prove protocol finality.

## 2. Calculate fees in asset units

Run:

```bash
yearn-data --db /path/to/yearn.sqlite index-fees --canonical --limit 500
```

Repeat the command while reports remain to be processed.
Older V2 reports can require verification of selected transactions.
See [fee accounting](fee-accounting.md) for report selection and request limits.

Use `recompute-fees` to recalculate from saved evidence without RPC requests.
Missing evidence must remain unavailable. It must not become a zero amount.

## 3. Fetch prices

Set `YEARN_PRICE_PROD_KEY` in the environment or a supported environment file.
See [earnings pricing](earnings-pricing.md) for service configuration.

Select one cutoff for both analyses.
The cutoff must be a closed UTC midnight, no later than today's UTC midnight.
Reports at or after the cutoff are excluded.

The following example uses today's UTC midnight.
Use an earlier midnight to select an earlier reporting boundary.

```bash
DB=/path/to/yearn.sqlite
CUTOFF=$(python3 -c 'import time; print(int(time.time()) // 86400 * 86400)')
yearn-data --db "$DB" price --source yearn-prices --no-provider-fallback \
  --no-onchain-fallbacks --limit 500
yearn-data --db "$DB" price-fees --before-timestamp "$CUTOFF" --limit 500
```

These commands use only Yearn Prices.
Without the explicit flags, earnings pricing can use DefiLlama as a fallback.
Fee USD valuation always uses Yearn Prices.

Repeat the limited pricing calls as needed.
Use these options when you need to update saved results:

| Option | Action |
| --- | --- |
| `--retry-missing` | Retry previously unsuccessful price results. |
| `price-fees --refresh` | Refresh selected fee prices that are already saved. |

A failed Yearn batch does not cause separate requests for each target or requests to another provider.
Fee pricing reuses one price per asset and day across contract families.
It keeps prices from different service endpoints and valuation policies separate.
Earnings retain a price record per report target.
Both paths request end-of-day prices.

Earnings pricing can save prices outside the selected cutoff.
The analysis still excludes reports at or after the cutoff.

## 4. Run both analyses

In the same shell, run:

```bash
yearn-data --db "$DB" analyze lifetime-yield --price-source yearn-prices \
  --no-provider-fallback --before-timestamp "$CUTOFF"
yearn-data --db "$DB" analyze fee-usd --before-timestamp "$CUTOFF"
```

Record the run ID printed by each command.
Each analysis reads the current stored data and prices.
Neither analysis proves that historical discovery is complete.

## 5. Export the completed runs

Use the recorded IDs and a new directory for each run.
Replace `EARNINGS_RUN_ID` and `FEES_RUN_ID` in this example:

```bash
yearn-data --db "$DB" export lifetime-yield --run-id EARNINGS_RUN_ID \
  --out /path/to/exports/lifetime-yield/EARNINGS_RUN_ID
yearn-data --db "$DB" export fee-usd --run-id FEES_RUN_ID \
  --out /path/to/exports/fee-usd/FEES_RUN_ID
```

An explicit run ID must belong to the requested analysis and be complete.
The exporter checks this before it writes files.
Without `--run-id`, it selects the latest completed run once.

### Check the export context

Each export includes CSV files and `context.json`.
The context file records the run ID, name, timestamps, parameters, and CSV file list.

Earnings parameters include the selected price providers and cutoff.
Fee parameters include the cutoff, valuation policy, and a hash that identifies the service endpoint.
If you supply a deferred-accounting manifest, its hash is also recorded.

The CSVs and context file always refer to the same completed run.

### Read the outputs

| Export | Contents |
| --- | --- |
| Earnings | Reports and summaries by vault, chain, and total. |
| Fee USD | Fee events and summaries by day, vault, asset, chain, and contract family. |

Raw amounts are integer strings in the asset's smallest unit.
USD amounts are decimal strings.
Missing accounting, missing asset details, missing prices, and known zero amounts remain separate.

Summaries show known subtotals and counts of events with unknown values.
A known subtotal can be incomplete. It is not necessarily the full revenue total.

Keep V2 vault, V3 allocator, and Tokenized Strategy fees separate.
They can charge fees at different layers of the same capital.
These are gross contract fees, not Yearn treasury receipts.
A missing price affects the USD value, not the stored raw fee amount.

Use `--deferred-manifest` only to label unresolved accounting with a validated manifest.
The manifest cannot supply a fee amount or override accepted current evidence.
