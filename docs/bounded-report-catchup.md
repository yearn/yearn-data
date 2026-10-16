# Fetch reports for a selected block range

Use `catch-up` to fetch reports for one chain.
Set the first and last block explicitly. Both blocks are included.

## Select the source

| Chains | Default source |
| --- | --- |
| Ethereum, Polygon, Base, Arbitrum, Katana | Envio |
| Optimism, Fantom | RPC |

Set the RPC endpoint for the selected chain.
The command uses RPC to check block details, even when reports come from Envio.
For Envio requests, also set `YEARN_ENVIO_GRAPHQL_URL`.

Use `--source rpc` to select RPC explicitly.
Use `--source envio` only on a chain with configured Envio coverage.
The command does not switch sources after a provider error.

## Run the command

Set `FROM_BLOCK` and `TO_BLOCK` before you run this example:

```bash
yearn-data --db data/yearn.sqlite catch-up --chain op \
  --from-block "$FROM_BLOCK" --to-block "$TO_BLOCK" --discover
```

Keep these block limits in your calling script.
To resume, run the command again with the same limits.
The command does not use a saved scheduler run.

## Select the finality policy

By default, `TO_BLOCK` must be at or below the RPC `finalized` block.
Envio must also have processed the requested range when it is the selected source.

If the RPC finalized block is unavailable or stale, you can select a confirmation buffer:

```bash
yearn-data --db data/yearn.sqlite catch-up --chain kat \
  --from-block "$FROM_BLOCK" --to-block "$TO_BLOCK" --confirmations 1000
```

This permits blocks up to the latest block minus 1,000 blocks.
It does not claim that the chain protocol has finalized those blocks.

The JSON result records the selected block number, hash, timestamp, and finality policy.
Keep this result with your acquisition records.

## Select the vaults

The command includes all stored Yearn vaults on the selected chain.
This includes inactive vaults and vaults absent from the current registry.

`--discover` also checks configured registries and role managers within the requested range.
It adds newly found vaults to the stored vault selection.
Use `--include-experimental-v2` to include experimental V2 registrations in this search.
Previously stored experimental vaults remain available on later runs.

This search does not prove that every historical vault has been found.
A newly found vault can have reports before `FROM_BLOCK`.
Fetch an earlier range if you need those reports.

The command asks Kong for missing asset details.
If necessary, it then requests historical details through RPC at the selected block.
It stops if required details remain missing or the vault release is unsupported.

## Resume after a failure

The command saves each successful range and its observations in one database transaction.
This also applies to ranges with no events.
A failed range has no success record.

Completed ranges can be reused, even if you change `--chunk-size`.
The command checks saved block hashes before it reuses coverage.
Matching event identities are not added again.
Conflicting amounts or hashes cause an error.

If a request fails:

1. Read the error.
2. If the provider rejects the range size or returns its 10,000-log limit, reduce `--chunk-size`.
3. Run the same block range again.

Investigate conflicting evidence before you retry.
The command does not split failed requests or retry indefinitely.

## Understand the scope

Stored reports and older cursors do not prove that an unscanned range is complete.
The command leaves those records intact and does not advance the older forward cursor.
It leaves existing coordinator tables intact but does not use a coordinator run.

Run fee accounting, pricing, analysis, and export separately.
Use a separate command for Tokenized Strategy fees.
`catch-up` does not fetch volume or debt flows, change financial outputs, or copy the database.
