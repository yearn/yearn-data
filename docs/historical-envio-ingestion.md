# Import historical reports from Envio

Use Envio to find Yearn vaults and import their V2 and V3 reports.
Use Kong to fill missing vault and asset details.

## Set the data source

Set `YEARN_ENVIO_GRAPHQL_URL` to the Envio endpoint.
Discovery and report imports use Envio by default.

To use the older RPC path, set `YEARN_DATA_EVENT_SOURCE=rpc`.
Volume and legacy fee indexing always use RPC.

The default chains are Ethereum, Polygon, Base, Arbitrum, and Katana.
Optimism and Fantom are configured for explicit historical RPC work.
They do not have a configured Envio source.

## Import a block range

1. Select one chain.
2. Set `FROM_BLOCK` to the first block to import.
3. Set `TO_BLOCK` to the last block to import. Envio must have processed this block.
4. Run discovery, then import the reports:

```bash
yearn-data --db data/yearn.sqlite discover --chains eth --include-retired --to-block "$TO_BLOCK"
yearn-data --db data/yearn.sqlite index-events --chains eth --include-inactive --from-block "$FROM_BLOCK" --to-block "$TO_BLOCK"
```

An import for a specific range requires both block limits.
Use `--versions v2` to select V2 reports.
Use `--vaults ADDRESS ...` to select specific vaults.

These commands do not check whether the RPC considers the last block finalized.
Select the upper block limit before you run them.

## Include historical vaults

Discovery keeps vaults that previously belonged to Yearn.
It does not mark retired vaults as active again.
Use `discover --include-experimental-v2` to include experimental V2 registrations.

A registration event does not prove when a vault was deployed.
The importer keeps reports that occurred before registration.
It does not assume token decimals when asset details are missing.

Earnings analysis can use stored reports from inactive vaults.

## Repeat or resume an import

You can repeat the same block range. Existing report identities are not added again.
The reported count includes fetched reports that were already stored.

The importer saves each completed window before it requests the next window.
A failed request does not advance the saved position.
An import for a specific range leaves the normal forward cursor unchanged.
A cursor is the saved position for later imports.

Imports without a specific range use one forward cursor per chain and event entity.
That cursor does not prove that newly added vaults have complete history.
Import an earlier block range when you add a vault that needs older reports.

## Handle invalid responses

The importer stops if a response has any of these problems:

- A required event entity is missing.
- A report has the wrong chain, address, or block range.
- A page does not advance beyond the previous page.

An invalid response is not treated as proof that a range has no events.
Address filters accept both lowercase and checksum address forms.

Run pricing, analysis, and export as separate commands after the import.
