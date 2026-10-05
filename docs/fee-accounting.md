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

For an explicitly scoped gross-fees-across-the-stack metric, a consumer can sum distinct accepted charges across families. Nesting alone does not make charges duplicates. Count each bound charge once; components and later distributions do not add another charge. Earnings recognized at multiple layers require a different consolidation policy. See the [Powerglove accounting contract](powerglove-accounting-contract.md).

## Recover fees from older V2 reports

Some older V2 reports need receipt and historical state evidence to calculate fees.
Use:

```bash
yearn-data --db /path/to/yearn.sqlite index-fees --canonical \
  --reconstruct-v2 --retry-unresolved --limit COUNT
```

Replace `COUNT` with the maximum number of reports to process.

Estimated bounds and price-per-share calculations remain candidate amounts.
They do not enter accepted totals.

A complete, supported receipt with no share mint can establish zero fees.
A mint of zero shares cannot establish zero fees.
A positive asset fee can round down to zero shares.

### Verify selected transactions

An execution trace records operations performed during a transaction.
The verifier uses this evidence to check the executed asset fee amount against the receipt.

1. Create a JSON list of `[chain_id, transaction_hash, report_log_index]` entries.
2. Save it as `/path/to/keys.json`.
3. Select a maximum number of trace requests.
4. Run:

```bash
yearn-data --db /path/to/yearn.sqlite index-fees --canonical \
  --report-keys /path/to/keys.json --verify-selective --verify-execution \
  --reconstruct-v2 --trace-limit COUNT
```

The trace limit applies to one invocation. Failed requests also count toward the limit.
An unavailable trace does not prove that a fee is zero.

Without `--verify-execution`, selective verification checks supported zero-gain mints
or later vault activity found around reconstructed reports.

### Recalculate from saved evidence

These commands do not make RPC requests:

```bash
yearn-data --db /path/to/yearn.sqlite recompute-fees policy
yearn-data --db /path/to/yearn.sqlite recompute-fees formulas
```

They preserve accepted execution amounts when the source evidence still matches.
They reject evidence if its source inputs have changed.
Conditional and unavailable results remain outside accepted totals.

Use `index-fees --canonical --filtered-evidence-only` to process complete saved V2 log evidence offline.
Filtered logs are a selected part of the event history. They are not treated as a full receipt.
Use `--refresh-evidence` when you need to fetch evidence again.
