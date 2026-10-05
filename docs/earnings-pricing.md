# Price historical earnings

The `price` and `analyze lifetime-yield` commands use Yearn Prices first.
By default, they can also use DefiLlama when a Yearn price is unavailable.

## Configure Yearn Prices

| Setting | Purpose | Default |
| --- | --- | --- |
| `YEARN_PRICE_PROD_KEY` | API key | Required |
| `YEARN_PRICE_PROD_BASE_URL` | Service address | `https://prices.yearn.dev` |
| `YEARN_PRICE_BATCH_TOKENS` | Tokens per batch | 20; maximum 50 |

Each batch segment requests at most 90 days per token.
HTTP requests allow three attempts by default.

## Use only Yearn Prices

Use these commands to price and analyze reports already stored in the database:

```bash
yearn-data --db data/yearn.sqlite price --source yearn-prices \
  --no-provider-fallback --no-onchain-fallbacks --limit 500
yearn-data --db data/yearn.sqlite analyze lifetime-yield \
  --price-source yearn-prices --no-provider-fallback
yearn-data --db data/yearn.sqlite export lifetime-yield --out exports/earnings
```

`--no-provider-fallback` prevents the use of DefiLlama prices.
This also prevents analysis from using an existing DefiLlama price in the database.
`--no-onchain-fallbacks` disables local price calculations that use onchain data.

These commands do not fetch onchain events again.
Volume and legacy fee analyses keep their existing DefiLlama policy.

## Understand price requests

The requested price time is 23:59:59 UTC on the report day.
The client checks the asset, timestamp, and positive price value.

If a successful batch response omits a target, the client makes an exact historical request for that target.
Network failures remain eligible for retry.
An authentication failure stops the run.

Saved price records include these details:

- The requested timestamp and the selected end-of-day timestamp.
- The upstream source and confidence value.
- The adapter used to obtain the price.

An adapter is the code path that obtains or calculates a price.
Use `--retry-missing` to retry targets with unsuccessful saved results.

## Read the earnings results

Earnings include all stored reports, including reports from retired vaults.
Reports remain in scope when vault details are missing.

The outputs retain raw gain, loss, and net amounts.
They also include amounts adjusted for known incidents, including the yRecoverer recovery accounting loss.

| Result | Meaning |
| --- | --- |
| Known zero amount | No price is required to value that amount as zero. |
| `null` for a nonzero amount | A USD value is unavailable. |
| Known USD subtotal | The sum of values that could be calculated. It can be incomplete. |

Summaries include priced and unpriced report counts.
A completed analysis is a saved snapshot.
Run the analysis again after you change relevant reports or prices.

## Optional DefiLlama and onchain prices

If you enable DefiLlama, the client selects the nearest valid price within 12 hours of the report timestamp.
It rejects prices outside that window.

Historical requests use the batch endpoint.
HTTP 429 responses trigger a limited number of retries.
The client follows `Retry-After`, with a maximum wait of 30 seconds.

Local adapters can calculate prices from these sources:

- Historical Yearn V1 share prices.
- ERC-4626 conversions.
- Balancer pools.
- Additional Curve wrappers.

These calculations can require archive RPC calls.
Missing reserve prices or token decimals leave the asset unpriced.
A zero reserve balance does not require a price.

Use `--no-onchain-fallbacks` to disable these calculations when you use DefiLlama.
The Yearn-only commands above do not call DefiLlama or these adapters.

### Refresh prices after an adapter correction

Run this command to replace saved adapter results:

```bash
yearn-data price --source defillama --refresh-onchain-fallbacks --limit 500
```

The `price-volume` command also accepts `--refresh-onchain-fallbacks`.
