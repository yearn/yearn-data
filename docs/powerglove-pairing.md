# Serve earnings and fees to Powerglove

The pairing reads completed `lifetime-yield` and `fee-usd` results. It does not collect events, fetch prices or change accounting during HTTP requests. Powerglove displays the amounts available in those results; developer diagnostics remain in yearn-data.

## Select a publication

Use the same database and explicit closed UTC cutoff for both analyses. Disable the earnings provider fallback for this pairing. See [the existing refresh workflow](earnings-and-fees.md) for collecting history and repairing prices before analysis.

Record the run IDs printed by the two analyses, then select them explicitly:

```bash
yearn-data --db data/yearn.sqlite select-pairing \
  --earnings-run-id EARNINGS_RUN_ID --fees-run-id FEES_RUN_ID \
  --out data/powerglove
```

Selection checks that both runs are complete, use compatible Yearn EOD pricing and the same cutoff, and contain the same V2/allocator report identities. It checks selected prices across shared asset/day targets and rejects duplicate event identities. Missing prices and unavailable accounting are retained; they do not prevent publishing the amounts that are known.

For the verified October 5, 2026 snapshot, the selected runs are earnings **13** and fees **12**, with exclusive cutoff `1791158400`. These are example IDs from the local canonical database, not defaults for another environment.

The local selection command is:

```bash
PYTHONPATH=src .venv/bin/python -m yearn_data.cli --db data/yearn.sqlite \
  select-pairing --earnings-run-id 13 --fees-run-id 12 --out data/powerglove
```

The publication directory contains:

- `current.json`: the selected dataset ID, replaced atomically after validation succeeds.
- `datasets/<dataset-id>.json`: source database path, completed run context, output fingerprints, cutoff, policies, financial scope, supported chains, frozen vault names and diagnostic counts.

A failed selection leaves the previous publication selected. Older dataset manifests remain available for clients finishing requests against an earlier selection. Keep the referenced database and its completed results available. Do not overwrite completed analysis outputs; produce new runs after data repairs.

## Run the read-only API

```bash
yearn-data serve-pairing --publication data/powerglove \
  --host 127.0.0.1 --port 3490
```

The server validates the selected results at startup, reads the source database in read-only mode, and caches bounded queries over those results. It makes no RPC or price-service requests. It supplies only:

| Path | Result |
| --- | --- |
| `/api/fees` | Fee and incident-adjusted reported-earnings amounts, with fee family breakdown. |
| `/api/fees/history` | Ordered monthly fee/earnings buckets. |
| `/api/fees/vaults` | Chain/address-keyed vault rows and their amounts. |

Filters are `since`, exclusive `until`, `chainId` (one ID or a comma-separated list), and `vaultAddress`. History supports `interval=monthly` (the unchanged default) or `interval=weekly`. Monetary values are decimal strings or null. A wholly unavailable amount is not converted to zero. Unknown chains and invalid parameters produce normal request errors.

`vaultAddress` selects one contract and requires exactly one `chainId`. Addresses are validated as 20-byte hex values and matched case-insensitively. The same selector applies before aggregation to all three views. A contract absent from the selected dataset on that chain produces HTTP 400; a known contract with no reports in the requested range returns empty history and unavailable totals. Missing names do not prevent selection.

Weekly buckets use Monday 00:00 UTC through the following Monday, with an exclusive end. Their `period` is the week-start date (`YYYY-MM-DD`), and `startTimestamp` / `endTimestamp` give those calendar boundaries in Unix seconds. They are sorted by week start. Existing monthly buckets retain their `YYYY-MM` periods and response shape.

Amounts are filtered by `[since, until)` and the selected dataset cutoff before grouping. A first or last weekly bucket can therefore contain only part of its calendar week; its boundary fields describe the calendar week, not additional observations. Empty calendar weeks are not fabricated. Weekly earnings remain reports recognized during the period rather than continuously accrued returns.

For example, these requests describe one vault's amounts and weekly history:

```text
/api/fees?chainId=1&vaultAddress=0x...&since=START&until=END
/api/fees/history?chainId=1&vaultAddress=0x...&interval=weekly&since=START&until=END&datasetId=SELECTED_ID
/api/fees/vaults?chainId=1&vaultAddress=0x...&since=START&until=END&datasetId=SELECTED_ID
```

Replace the placeholders with a full address, Unix timestamps and the summary's dataset ID. Vault filtering counts that contract's own accepted fee charges and incident-adjusted reported P&L. It does not consolidate children into the parent or attribute downstream charges to historical ownership. Tokenized Strategy fees remain available independently of main vault earnings.

Each response carries an opaque `datasetId`. Powerglove uses the summary's ID in subsequent history/vault requests, so a manual publication change cannot mix datasets. That identifier is not displayed. Source reasons, price provenance, coverage counts and reporting cutoffs remain in developer publication records and the existing analysis exports.

If the production frontend calls a separate API origin, pass its exact origin with `--cors-origin`. Otherwise use a same-origin reverse proxy. No Yearn Prices or RPC credentials belong in the display contract.

## Connect Powerglove without replacing its other sources

The Powerglove changes are based on `codex/qtov-on-improved-fee-data@04a1a30586d17a3b3da86ccdd30ecf2560a2435a`, on continuation branch `ross/yearn-data-pairing`.

Configure the new source separately:

```dotenv
# Production origin for only the three core fee/earnings views.
VITE_PUBLIC_YEARN_DATA_API_URL=https://your-data-service.example

# Local Vite preview target for those same views.
VITE_YEARN_DATA_API_TARGET=http://127.0.0.1:3490
```

Keep `VITE_PUBLIC_YEARN_FEES_API_URL` and `VITE_YEARN_FEES_API_TARGET` on the existing fee-analytics source. `/api/fees/stack` and `/api/profitability` continue using it. Keep the TVL variables on the existing TVL source. Without the new Yearn Data variables, core views retain the existing fee-origin fallback.

The local Vite proxy routes `/api/fees/stack` before the broader `/api/fees` prefix. This ordering preserves fee-stack analytics while the three core views use yearn-data. Production uses the same split in the request helper. Changing the new source does not move analytics or TVL requests.

Rebuild Powerglove after changing public build variables. For a local production preview:

```bash
bun run build
bun run preview --host 127.0.0.1 --port 4598 --strictPort
```

Review `/stats?tab=fees`. The fee cards and monthly/cumulative charts use the selected yearn-data publication. Fee Analysis and TVL vs Fee Yield retain their existing sources. Loading, filter changes and request retry remain ordinary UI states. Canonical-coverage counters and missing-source warnings are absent from the public Fees panel.

## Refresh and diagnose

1. Repair or extend source data using the existing bounded ingestion/accounting/pricing commands as needed.
2. Run both analyses with the chosen cutoff and Yearn-only policy. Record their IDs.
3. Export each explicit completed run with `export ... --run-id ...`; retain its context and event/report rows for diagnosis.
4. Run `select-pairing` with those IDs. Successful selection atomically activates them; no server restart is needed for a new dataset.
5. Verify the three served views against the selected outputs. Clients can finish requests against the preceding ID.

For a revaluation using the same October 5 cutoff, these commands create new completed runs without collecting history or fetching prices. Substitute their newly printed IDs in the exports and selection command; do not assume they remain 13 and 12:

```bash
yearn-data --db data/yearn.sqlite analyze lifetime-yield \
  --price-source yearn-prices --no-provider-fallback --before-timestamp 1791158400
yearn-data --db data/yearn.sqlite analyze fee-usd --before-timestamp 1791158400
yearn-data --db data/yearn.sqlite export lifetime-yield --run-id EARNINGS_RUN_ID \
  --out artifacts/pairing-refresh/lifetime-yield/EARNINGS_RUN_ID
yearn-data --db data/yearn.sqlite export fee-usd --run-id FEES_RUN_ID \
  --out artifacts/pairing-refresh/fee-usd/FEES_RUN_ID
```

Use `fee_usd_events.csv` and the fee rollups for acceptance reasons, unavailable metadata/prices and component counts. Use `reports.csv` for raw/adjusted earnings, incident rules and price provenance. The selected manifest links those records to the actual publication. Price repairs and fee-execution repairs remain separate operations; neither requires adding diagnostic displays to Powerglove.

This delivery retains reported P&L rather than consolidating historical yield across nested contracts. Distinct accepted fee charges across the included families are summed once. See the [accounting contract](powerglove-accounting-contract.md) and [pairing requirements](powerglove-pairing-requirements.md).

## Validation record

The implementation is validated with offline selection/filter/accounting fixtures, the repository suites, a production Powerglove build, and an actual-source browser preview. The local detailed receipts, source reconciliation, preserved-analytics comparisons and screenshot are under `artifacts/powerglove-pairing-validation/` and are intentionally outside Git. Private preview URLs belong in the chat, not repository or pull-request content.

| Requirements check | Evidence |
| --- | --- |
| A1: selection | Completed-run, cutoff, policy, cohort, duplicate and shared-price checks; failed selection retains the preceding manifest; pinned requests survive refresh. |
| A2: filters | Inclusive/exclusive fixture boundaries and UTC monthly grouping; native rollup reconciliation across 8 chains and 740 vaults; 30D/90D/1Y source-row comparisons and all-time preview. |
| A3: fee identity | Nested $10 + $9 charges remain $19; components are separate projections of accepted native event amounts. Existing accounting tests retain report binding and acceptance behavior. |
| A4–A5: earnings | Existing retired-history and incident tests; independently priced earnings with deferred fees; Tokenized charges included without adding Tokenized P&L to main earnings. |
| A6: diagnostics | Run-linked manifests and original report/event outputs retain accounting/pricing status and provenance. Zero-fee and unavailable-component fixtures remain distinct. Shared priced evidence matches across 40,400 asset-days in the selected publication. |
| A7–A8: rendering/requests | Actual-source cards/charts/table; browser HTTP failure and retry; absent amounts render as dashes; filter/late-response/cache-refresh tests; no public coverage panel. |
| A9: analytics | Proxied fee-stack and profitability financial payloads match their existing providers; TVL continues serving successfully; production-origin separation is tested. |
| A10: pairing | Production frontend build, actual-source browser inspection, dataset-pinned history/vault requests and retained private preview. |
