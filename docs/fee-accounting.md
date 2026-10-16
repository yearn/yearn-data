# Calculate fees in asset units

Use canonical fee accounting to calculate fees from stored allocator reports.
The results use the vault asset's units. They do not include USD prices.
Reports from retired vaults remain in scope.

## Calculate allocator fees

Run:

```bash
yearn-data --db /path/to/yearn.sqlite index-fees --canonical
```

The command reads V3 fee fields and V2 transaction receipts.
A receipt records the result and logs of a transaction.

Results with insufficient evidence remain unavailable.
Use `--retry-unresolved` to retry those results.
Use `--version v3` to calculate V3 fees from stored reports without RPC requests.

## Analyze and export the results

Run:

```bash
yearn-data --db /path/to/yearn.sqlite analyze canonical-fees
yearn-data --db /path/to/yearn.sqlite export canonical-fees --out /path/to/exports
```

The outputs contain fee events, accounting coverage, and subtotals by asset.
Accounting coverage describes the stored reports that could be processed.
It does not prove that all historical events were collected.

Keep these states separate:

| State | Meaning |
| --- | --- |
| Known zero | Evidence supports an amount of zero. |
| Unavailable | Evidence is insufficient to calculate an amount. |
| Not applicable | This fee component does not apply. |

A reported gain of zero does not always prove that fees are zero.
V2 releases without a supported zero-gain rule still require fee evidence.

## Collect Tokenized Strategy fees

Tokenized Strategy fees have a separate output family.
A family identifies the type of contract that charged the fee.

Supply a JSON list of classified contracts:

```bash
yearn-data --db /path/to/yearn.sqlite index-tokenized-fees \
  --inventory /path/to/inventory.json --chain eth \
  --from-block START --to-block END --before-timestamp CUTOFF \
  --max-vaults COUNT
```

Replace `START`, `END`, `CUTOFF`, and `COUNT` with your selected limits.
`CUTOFF` is an exclusive Unix timestamp: events at or after it are excluded.

Omit `--inventory` to use the maintained list included with the package.
This list keeps retired contracts, but it is not proof that every contract has been found.
The command rejects unsupported chains instead of selecting another chain.

The command saves each complete block chunk in one database transaction.
It does not count the same event twice.
When overlapping results agree, it keeps the original evidence.

## Select the finality policy

By default, the command uses the RPC `finalized` block as its upper limit.
Use `--confirmations 1000` to use the latest block minus 1,000 blocks instead.

The command records this choice in its output and saved range evidence.
A confirmation buffer does not prove protocol finality.

## Keep fee families separate

Allocator and Tokenized Strategy fees can occur at different layers of the same capital.
For example, a strategy can charge a fee on assets held through an allocator vault.

These outputs report gross contract fees. They do not measure receipts of the Yearn treasury.
Keep the contract family when you interpret or combine results.
Do not treat the family subtotals as independent treasury revenue.
