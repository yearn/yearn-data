# Powerglove remaining service cutover requirements

Status: implemented on the committed Neon/Postgres base; live validation and remaining source-coverage limits are recorded in [the operator guide](powerglove-analytics.md).

## Outcome and boundary

Powerglove's `codex/stats-page` must obtain the remaining actively used TVL and fee analytics from Yearn Data. Yearn Data must own the inputs needed to produce those results in the hosted Neon Postgres database selected by `NEON_DB_URL`, including DefiLlama reference observations. Running the supported stats views must no longer require either legacy service or its SQLite database.

This extends the completed core fees, reported earnings and TVL pairing. It does not replace their accounting definitions. Preserve the existing UI sections, filters, historical bridge exclusions, pricing rejection rules, and unrelated Powerglove work.

| Endpoint | Observable result |
| --- | --- |
| `/api/comparison/defillama-comparable` | Vaults & Curation can identify locally counted vaults absent from the verified DefiLlama comparable set. |
| `/api/fees/stack` | Fee Analysis can display nested relationships, allocated capital, configured fee rates and effective fee rates. |
| `/api/profitability` | TVL vs Fee Yield and associated classifications and trends use published fees and time-weighted historical capital. |
| `/api/comparison` | Existing aggregate comparison uses Yearn Data-owned DefiLlama snapshots. |

Developer availability, coverage and provenance belong in Yearn Data. Powerglove receives usable values and handles unavailable values safely; this work does not add public data-quality badges, explanations or a diagnostics dashboard.

## Starting point before this implementation

- The three new routes are absent from the current Yearn Data API. The comparable-membership route also needs implementing rather than copying an existing working legacy route.
- The hosted Postgres database stores TVL observations, nesting positions and valued fee/report outputs, but currently has no DefiLlama reference tables or fee-configuration snapshots. Actual charged fees do not establish configured fee rates.
- `TvlDataset.comparison()` in [tvl_api.py](../src/yearn_data/tvl_api.py) currently reads `defillama_snapshots` from the separate database configured by `--comparison-db`.
- Powerglove's consumer contracts are in `src/components/landing/native-stats/AuditPanel.tsx`, `types.ts` and `FeesPanel.tsx`. Its routing keeps these supplementary analytics on legacy origins.
- The existing fee-stack implementation recognizes direct vault-address matches. The new implementation must use stored, verified relationships and preserve intermediary paths where those paths are known.

## R1. Stored inputs and publication identity

Postgres is the serving source. Keep `NEON_DB_URL` server-side and use the logical database reference `neon` in publications. The already-imported TVL, fees and earnings history must be reused; do not rerun the historical migration. SQLite may remain an optional local test/rollback source, with no production serving dependency. Prepared analytics and their immutable selection pointer must live in Postgres so the added routes can be reproduced without local publication artifacts.

Collect missing inputs independently of HTTP requests. Serving these routes must remain read-only and must not call an RPC, Kong, a pricing provider or DefiLlama on demand.

Store DefiLlama aggregate references and vault membership observations, plus dated fee-configuration observations. Retain source identity, observation time, collection status and sufficient provenance to reproduce the selected result. Fee observations must distinguish known zero from missing configuration and identify the contract or Accountant and any strategy-specific scope to which a rate applies.

Each response must expose an immutable analytics publication ID, calculation/observation timestamps, a methodology version and the contributing TVL dataset, fees/earnings dataset, reference snapshot and configuration snapshot IDs, where applicable. Requests must support pinning the analytics publication so that related calls remain coherent after a refresh. The implementation may reuse existing publication machinery; it must not make each endpoint independently choose mutable latest inputs.

Select the most complete coherent inputs using the established known-value-first policy. A newer failed or partial attempt must not displace known observations simply because it is newer. Freeze the selected inputs and their coverage into a publication; retain the last usable publication after a failed refresh. A new collection must not change responses pinned to an older publication.

Expose explicit developer-facing availability and coverage states, distinguishing available, partial and unavailable results and recording the affected scope and reason. Serve known amounts when other amounts are missing. Missing values remain null, not fabricated zero; partial aggregates must identify the known subtotal and its coverage. Publication must not claim complete coverage for unsupported or failed inputs.

## R2. DefiLlama ownership and membership comparison

Yearn Data must collect and retain the `yearn-finance` and `yearn-curating` aggregate references needed by `/api/comparison`, including chain values and observation timestamps. The API's ordinary configuration must use those stored references without opening the legacy TVL database. A one-time import may bootstrap history, but future refreshes must be owned by Yearn Data. Store these inputs and prepared analytics in Postgres; the serving process must not open a legacy SQLite database.

Vault inclusion requires a separately evidenced membership set. Do not derive it from aggregate totals, the local vault catalogue alone, or absence from a missing-vault list. Freeze the relevant DefiLlama adapter revision or equivalent authoritative membership evidence, its selection/exclusion rules, covered chains and observation times. Account for adapter-specific V1 handling, curation membership and nested/cross-asset exclusions where they affect the comparable universe. If membership is reconstructed from an adapter rather than supplied by DefiLlama, record that method explicitly in developer provenance.

`GET /api/comparison/defillama-comparable` must return `diff.missingFromDefillama`. Each entry must identify `vaultAddress`, `chainId`, `chainName` and `countedTvlUsd`; include the name/category when known. Use chain plus normalized address as identity. `countedTvlUsd` must be the selected local external TVL after the same nesting and dated bridge deductions used by the native audit and headline views.

Include a vault in that list only when it has positive, known locally counted TVL and is verified absent within the covered external membership scope. Unsupported chains or failed membership collection are unknown, not proof of absence. Expose the covered scope and verified membership so the consumer can distinguish confirmed inclusion from unknown membership.

Accept `includeVaultBreakdown=false`. It may suppress additional detailed breakdowns but must retain `diff.missingFromDefillama`, because that is the current UI contract. Preserve the existing aggregate `/api/comparison` fields and add provenance/coverage metadata without changing their units.

Powerglove must show an inclusion indicator only when membership is established. A failed request, uncovered chain or partial membership set must not cause every vault outside the missing list to be labeled included.

## R3. Fee stack

`GET /api/fees/stack` must preserve the consumer's tree and summary shapes: `chains`, root and child nodes, `maxDepth`, `effectivePerfFee`, `effectiveMgmtFee`, and the existing summary statistics. Nodes must identify chain/address/name, `capitalUsd`, `perfFee` and `mgmtFee`. Fee rates are basis points; capital is USD. Unavailable financial fields may be null, with matching consumer type/formatting changes.

Build the stack from the selected current relationships and allocated capital. Keep V2 vaults, V3 allocators and V3 Tokenized Strategies identifiable. Follow verified intermediary relationships instead of assuming every strategy address is itself a vault. Retain unresolved paths in developer coverage; do not fabricate children or silently claim a complete tree.

Use observed fee configurations applicable to each node or allocation. Unknown configurations, custom Accountant behavior and strategy-specific fees must not become a default uniform rate. Collect the configurations required for the supported published universe; explicitly report configurations that cannot be represented as fixed rates.

For supported fixed-rate performance-fee paths, compose successive charges as `1 - product(1 - rate)` and weight path rates by the capital attributable to that path, including known unallocated/leaf capital. Management-fee composition must document its annual time basis and capital bases; use a summed rate only where those assumptions support it. Identify these as effective configured rates, rather than realized fee revenue or an unconditional forecast.

Cap attributed ownership against the selected relationship and vault amounts. Detect cycles and bound traversal; a truncated or unresolved path must affect coverage and dependent effective-rate availability. Define depth and summary weighting in the methodology, and ensure `totalStackedCapital` does not count the same attributable capital repeatedly merely because several tree roots share it. Keep the full set of nodes available for vault-level analysis.

Fees taken at different layers are distinct charges. Do not deduplicate actual fees because a Tokenized Strategy is nested under an allocator. Conversely, do not sum nested strategy and allocator gains as consolidated economic yield.

## R4. Profitability and trends

`GET /api/profitability` must preserve the fields consumed by `FeesPanel`: per-vault identity/category, TVL, annualized fee revenue, fee yield, gain yield, fee capture, configured fee rates, totals, report count, harvest frequency, pricing confidence, trend and quadrant classification; retain the consumed summary, chain/category and quadrant groupings. Support optional `chainId`, validating it consistently with the existing API and applying it before aggregation.

Use published actual charged-fee valuations and incident-adjusted contract-level reported gains/losses. Do not substitute configured rates for realized fees. Keep the fee revenue scope explicit: charged fees are not automatically treasury or protocol revenue. Preserve retired-vault financial history rather than filtering all historical inputs by today's active flag.

Use a documented trailing-year period ending at the latest supported common cutoff of the selected financial and TVL inputs, not the server's wall clock. Expose the exact half-open period boundaries and contributing cutoffs. Current displayed TVL must be identified separately from the capital denominator used for yield.

Calculate time-weighted TVL from historical capital observations over that period. Define the daily/inter-observation weighting policy, elapsed time, treatment of pre-deployment dates and treatment of gaps. Missing days must not become zero or be silently filled through arbitrary outages. Record observed duration, expected duration and denominator coverage; publish eligibility thresholds and apply them consistently. Known zero capital is distinct from unavailable capital.

Annualize charged fees using the same supported elapsed period used for the time-weighted denominator. Compute fee yield as annualized fees divided by time-weighted capital. Define gain yield and fee capture with their exact gain basis and matching period. Unsupported, zero or inappropriate denominators must yield null rather than infinite or misleading ratios. Gains remain contract-level reported P&L; no consolidated underlying yield is promised by this route.

Trend calculations must compare adjacent supported periods, expose those boundaries and use consistent capital and fee methodology in both. Preserve the consumer's `improving`, `declining`, `stable` and `insufficient_data` categories; document the minimum report/history requirements and change thresholds. Insufficient history must not become a stable or improving trend.

Pricing confidence must derive from the selected valuation coverage and provenance, with documented `high`, `medium` and `low` thresholds. It must not imply that every quote is correct simply because it exists.

Compute quadrant thresholds from eligible vaults within the requested scope using the documented chart capital and yield measures. Match the displayed current-TVL cohort when deriving chart thresholds; keep the fee-yield denominator time-weighted. Missing fee observations cannot be classified as known zero, and a zero median cannot make genuine zero yield high yield. Ineligible vaults must have no fabricated quadrant. Document aggregate denominators separately: an external protocol-TVL denominator must use native nesting deductions, while any gross contract-capital aggregate must be labeled as such and must not be presented as consolidated TVL or economic yield.

## R5. Powerglove routing and operation

Move the three active supplementary requests onto the configured Yearn Data origin and local proxy target. Pin the selected analytics publication and update consumer types/helpers only as needed to preserve honest values, membership indicators and classifications. Retain existing environment-variable fallback behavior when Yearn Data is not configured.

Legacy graph, overlap, vault-list and older audit routes have no current UI callers. Their implementation or removal is outside this work; leave their fallback configuration alone. Successful cutover of the supported stats page must nevertheless be demonstrated with both legacy services stopped or unreachable and no legacy database configured.

Provide reproducible collection, validation, publication and API startup commands. A developer must be able to refresh these inputs and inspect missing coverage without guessing which legacy job or database is still required. Do not add production deployment or a new scheduling platform to this delivery.

## Acceptance checks

| Check | Evidence required |
| --- | --- |
| A1. Owned source data | Collection/refresh produces stored reference and fee-configuration observations in the hosted Neon Postgres database selected by `NEON_DB_URL`; API requests need no provider calls or legacy database. |
| A2. Membership | Fixtures cover verified inclusion, verified absence, chain/address collisions, excluded/zero-counted vaults and unavailable membership. `includeVaultBreakdown=false` retains the missing list, and the UI never infers inclusion from a failed/partial response. |
| A3. Stack | Reconcile simple, nested, intermediary and branched allocations; demonstrate bps composition, capital weighting/caps, known-zero fees, unavailable configuration and cycle/truncation handling. Shared capital is not duplicated in the stack capital summary. |
| A4. Profitability | A vault whose capital changes during the period yields the expected time-weighted denominator, differing from the latest snapshot denominator. Check partial history, missing pricing, zero capital, retired history, trends, eligibility and chain filtering. |
| A5. Accounting | Distinct fees charged at two layers remain distinct; reported gains do not become summed underlying yield. Existing TVL, core fees/earnings, bridge deductions and price rejections retain their accounting behavior. |
| A6. Publication | Responses identify coherent inputs and cutoffs. A refresh or failed collector leaves pinned results unchanged and preserves the last usable publication; diagnostics distinguish partial/unavailable from genuine zero. |
| A7. Consumer cutover | In a working Powerglove preview, Vaults & Curation, Fee Analysis and TVL vs Fee Yield load from Yearn Data with both legacy services unavailable. Existing aggregate comparison also works without their database. Verify browser requests, formatting and classifications. |

## Expected implementation boundary

1. Yearn Data: Postgres schema for owned observations; collectors/import support; immutable analytics publication and developer diagnostics; three route implementations and the existing comparison's source change; focused accounting/contract tests and operator instructions.
2. Powerglove `codex/stats-page`: request routing/proxy changes, publication pinning, required nullable types and value handling, and verified membership/classification handling. Preserve unrelated layout changes.
3. No replacement chart families, treasury attribution, general historical yield consolidation, broad historical fee-configuration backfill, upstream pricing fixes, unused legacy-route migration, public quality dashboard or production deployment.

The [operator guide](powerglove-analytics.md) records the stack capital/management-rate assumptions, profitability gap/eligibility policy and membership reconstruction method used by this implementation. These are implementation decisions to validate against stored evidence and the consumer contract, not permission to fabricate unavailable inputs.
