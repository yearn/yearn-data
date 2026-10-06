# Powerglove pairing requirements

Draft for implementation · October 5, 2026. Revised to keep data-quality diagnostics in yearn-data.

This document defines the first usable pairing between yearn-data and Powerglove. The [accounting contract](powerglove-accounting-contract.md) supplies the underlying fee-versus-earnings rules. This document specifies the consumer behavior, implementation boundary and checks needed to deliver that pairing. It does not authorize deployment or implement the API.

## Outcome and scope

Powerglove must show fee summaries, monthly history and per-vault amounts published by yearn-data, including active and retired history. Yearn-data owns data quality: developers must be able to identify unavailable accounting/prices, understand the reasons and repair the underlying data. Powerglove users receive the published values through a simple presentation contract.

Confirmed requirements from this work:

- Build on `ross/pr-08-fantom-replay`, retaining its existing accounting, pricing, cutoff and export behavior. The continuation checkout is `ross/powerglove-accounting`.
- Include active and retired vault reports together. Current status must not erase historical earnings.
- Count distinct accepted fee charges at every included layer once. Nesting alone does not make two fee charges duplicates.
- Keep earnings recognition separate from fee charges. Underlying performance can be recognized by multiple contracts, including V2-to-V3 nesting.
- Retain missing accounting, missing prices, reasons and cutoff information in yearn-data for developer diagnosis and repair. Serve the amounts we have without adding public completeness notices throughout Powerglove. Do not fabricate unknown source values as zero.

Recommended first-delivery boundary:

- Serve one explicitly selected set of completed earnings and fee results through the three core endpoint paths already requested by Powerglove.
- Offer gross fees across the included stack, retaining the family breakdown. Use incident-adjusted **reported P&L** for earnings; document its definition and the remaining consolidation work in yearn-data.
- Keep TVL, fee-stack and profitability behavior on their existing sources while migrating the three core views. Rebuilding those analytics in yearn-data is outside the first delivery; their removal or disabling is not a requirement.
- Provide a repeatable manual refresh and local preview. Automatic scheduling and production rollout are later steps.

These choices are the proposed implementation scope, not proof that a consolidated lifetime-yield metric already exists. If the intended first delivery must instead show consolidated underlying yield, historical attribution becomes required additional work; see the scope-expansion rule below.

## Verified starting point

| Component | Observed state | Implication |
| --- | --- | --- |
| yearn-data base | `ross/pr-08-fantom-replay@5fcea03f2803d6d4fed761aaea1d03ddb9d16965` | Already includes retired reports and native closed-day earnings cutoffs; do not reapply the older active-filter patch. |
| Powerglove consumer | `codex/qtov-on-improved-fee-data@04a1a30586d17a3b3da86ccdd30ecf2560a2435a` | Core fee requests already exist; response types and displays need a coordinated adaptation. |
| Canonical data | `data/yearn.sqlite`; completed earnings run 13 and fee-USD run 12 | Both record exclusive cutoff `1791158400`, October 5, 2026 at 00:00 UTC. Treat these as starting snapshots, not permanent run IDs. |
| Earnings snapshot | 96,881 reports; 30,723 retired; 4,541 unpriced | Inclusion is verified, but monetary totals remain known priced subtotals. |
| Fee snapshot | 117,248 stored events; 5,275 unpriced fee values; 181 accounting-unavailable events | Preserve accounting gaps independently from pricing gaps. Accounting-unavailable does not mean every event has the same deferral reason. |
| Existing outputs | Earnings report/summary CSVs and fee event/day/vault/asset/chain/family CSVs, plus run context | Reuse completed results and existing projection functions rather than building another acquisition pipeline. |

These counts were read from the local canonical snapshots while preparing this document. They do not establish complete historical discovery or deployment. Regenerated earnings and branch-validation evidence are retained under `artifacts/lifetime-earnings-regenerated-20261005/`.

## Functional requirements

### R1. Select and serve coherent completed results

The serving layer must use an explicit earnings run ID and fee run ID, both complete and belonging to the expected analyses. Their cutoffs and declared inclusion/pricing policies must be compatible. Do not select the latest run independently for each HTTP request or infer compatibility merely because two run IDs are adjacent.

Yearn-data must record the selected dataset, earnings/fee run IDs, exclusive UTC cutoff, generation time, included families, discovery scope and price/methodology evidence. Keep these details in the publication context and developer-facing records. The public response needs the requested monetary views and only the identifiers needed for request/cache consistency; it need not carry diagnostic counts or reasons. Powerglove need not display the dataset context or cutoff.

For the initial pairing, select Yearn-only earnings with fallback disabled and the existing Yearn EOD fee policy. Check the two caches' selected evidence for shared asset/day targets; investigate price/policy discrepancies in yearn-data rather than teaching the UI to explain them. Preserve the actual selected evidence for developer review. A shared provider name alone is insufficient.

HTTP reads must be read-only and bounded to the selected results. Requests must not collect events, reconstruct fees, fetch prices or run database migrations. Refreshes must publish a complete selected dataset as one unit; an interrupted refresh must leave the previous selected dataset usable.

Here, a completed dataset means the selected analysis runs and publication finished. It does not require every historical price or accounting observation to be available before known amounts can be served.

### R2. Supply the consumer's three core views

| Endpoint | Required behavior |
| --- | --- |
| `GET /api/fees` | Return published fee amounts, incident-adjusted reported earnings and the financial family breakdown for the selected filters. Keep data-quality diagnostics in yearn-data. |
| `GET /api/fees/history` | Return ascending UTC monthly buckets with published fee and earnings values. Preserve Powerglove's completed-month convention. |
| `GET /api/fees/vaults` | Return a count and vault rows keyed by `(chainId, lowercase address)`, with published fee amounts, reported earnings and available name/family metadata. A missing name must not remove financial history. |

Support the filters used by the first delivery: `since`, exclusive `until`, `chainId`, and `interval=monthly` for history. Use Unix seconds and `[since, until)` semantics. With no `since`, include all stored history within the selected scope. Cap the effective end at the dataset cutoff and record the applied range in yearn-data. This does not require a public cutoff or incomplete-range notice.

Define and validate an explicit family scope for fees. Its initial combined view includes V2 vault, V3 allocator and Tokenized Strategy charges and retains their separate amounts. Main reported earnings use the existing V2/allocator report universe; Tokenized P&L must not be added to that series. Additional family/vault selectors can be added only where the UI actually uses them.

Apply time/chain filters before grouping. Day rollups grouped only by family cannot answer chain-filtered monthly history; use completed event/report rows or a directly derived equivalent. Invalid request parameters must produce a normal API error. Yearn-data must distinguish unsupported or unscanned scope from genuine zero activity internally and avoid manufacturing a successful zero total for a wholly unavailable scope.

The endpoint paths are retained to reuse the current consumer. Full compatibility with the TypeScript reference service, its unused routes or every legacy coverage field is not required. Update producer and consumer types together.

### R3. Preserve distinct fee charges and their acceptance

Only accepted accounting contributes to fee amounts. Conditional, rejected, deferred and unindexed reports remain excluded from accepted fees and recorded in yearn-data diagnostics. Preserve each retained reason where available; Powerglove does not need those reason codes or counts.

Deduplicate representations of one charge using the established chain/transaction/log/report binding. Do not deduplicate distinct charges because their amount, asset, time or nesting relationship matches. Preserve raw units, asset identity and contract family.

Protocol/manager/performance components already contained in a total are not extra charges. Recipient distributions do not add another fee. Gross charges across the included stack must be labeled as such; they are not automatically treasury/protocol revenue. Unknown components stay unavailable even when the overall fee is known.

### R4. Preserve reported earnings independently of fee evidence

Include active and retired stored reports. Retain records with missing vault metadata. Preserve raw gains/losses/net amounts, existing incident adjustments and their provenance. USD values must follow the selected earnings policy and cutoff.

A report's earnings must not disappear merely because its fee accounting is unavailable. Fee and earnings known/unknown counts are independent developer diagnostics in yearn-data. Do not substitute gains/losses from accepted fee projections for the earnings report universe.

For this delivery, describe the main earnings series as incident-adjusted reported P&L. Its net amount is adjusted reported gains minus losses, not a promise of earnings after every fee in the stack. Do not label its aggregate as consolidated underlying yield or depositor net earnings. Keep contract/family identity available so later historical attribution can be added without changing raw records.

The old consumer's separate `tokenizedStrategyYield` field must not be filled with zero or folded into main earnings to satisfy a type check. Keep its definition separate in the backend. A missing optional projection does not require adding a public diagnostic panel.

### R5. Keep data-quality diagnostics and repair work in yearn-data

Aggregate accepted available amounts with decimal arithmetic. Preserve unknown source values as null internally; do not invent prices, fee evidence or amounts. A known aggregate can be published as the amount currently available even when some source events remain unresolved. This first pairing does not require public partial-subtotal labels or complete historical coverage before serving useful data.

Yearn-data must retain independent accounting, metadata, pricing and historical-coverage diagnostics, including reasons, affected event/asset/day identities, counts, cutoff and selected evidence. Developers need enough information to investigate and repair each category. Reuse existing CSVs, run context and diagnostic artifacts; do not build another diagnostics service solely for this pairing.

Keep diagnostics reconcilable to the selected publication and its filters. A price repair must lead to a reproducible revaluation and new publication. Accounting-evidence repair remains a separate operation. Existing missing-price targets and unresolved-fee records remain developer work items even while known amounts are served.

Powerglove does not need reason codes, coverage counts, confidence explanations, discovery flags or cutoffs to render the published data. A wholly unavailable aggregate may remain null; use the usual missing-value treatment rather than fabricate a number.

### R6. Display published values with normal request handling

Powerglove must adapt its response types/validation to the simpler display contract: decimal monetary values, ordinary null handling, financial scope and the identifiers necessary for stable requests. Render the values yearn-data publishes in the existing cards, tables and charts.

Do not require public coverage badges, missing-event counters, incomplete-history banners, price-source explanations, partial-subtotal warnings or a dataset-cutoff display. Remove the old Fees panel's developer-oriented canonical-coverage presentation from the pairing contract. Keep diagnostic reasons in yearn-data.

Cumulative charts may sum the published known amounts. Missing source events do not require nulling the remainder of the cumulative series or propagating a public partial-state flag. Preserve monthly sorting, selected-timeframe accumulation and the completed-month convention. A wholly absent monthly amount can use existing missing-value behavior; do not invent an event amount or price to fill it.

Retain normal loading, empty, HTTP error/retry and filter-change behavior. These request states are different from diagnostics about individual missing source events. Old responses/cached data must not overwrite or mislabel the selected filter or publication. Existing product refresh indicators can remain; adding an operational cutoff notice is not required.

### R7. Preserve the separate TVL connection

Use a separate core origin through `VITE_PUBLIC_YEARN_DATA_API_URL` in production and `VITE_YEARN_DATA_API_TARGET` for the local proxy. Preserve the existing fee-origin variables for fee-stack/profitability; core views can fall back to that existing source when the new variables are unset. This split avoids moving existing analytics when the core source changes. Verify the client-appended `/api/fees` paths in both modes. Configure the required same-origin proxy or CORS without exposing Yearn Prices/RPC credentials to the browser.

TVL requests and their provider must continue working independently. An unused required `tvlUsd` field in the old fee-row type must become optional rather than receive an invented zero. Preserve the existing fee-stack/profitability features on their current source while the core views move to yearn-data. These requests currently share the fee API origin, so inspect and separate their routing as needed; a blanket origin switch must not send them to a service that lacks them. Verify the existing source before cutover. If it cannot be retained, record a separate product/source decision before changing those features; this document does not prescribe removing or disabling them.

### R8. Make refresh and review reproducible

Document the selected source database, price policy, cutoff, completed run IDs and exact commands used to regenerate and export the paired dataset. Use the existing native `--before-timestamp`, `--no-provider-fallback` and export `--run-id` support. New runs must not overwrite earlier analysis snapshots or alter raw events/accepted accounting as a side effect of publication.

Reconcile served views against the pinned outputs, retain the dataset context in developer publication records, and provide a Powerglove preview using the new source. Source-code checks alone and an HTTP 200 do not demonstrate consumer correctness.

## Behavior to preserve

- Existing native fee arithmetic, acceptance decisions, reconstruction evidence, incident rules and price provenance.
- Active/retired inclusion and the requested base's cutoff/export functionality; the regression tests already added protect these.
- Existing explicit provider options for other workflows. Choosing Yearn-only for this pairing does not change the repository's global fallback default.
- Existing CSVs, completed analysis runs and raw tables. Consumer JSON is an additional projection.
- Powerglove's time/chain filtering, monthly chart convention and straightforward numeric presentation. Existing developer-quality panels may be removed from the public Fees tab as part of this contract adaptation.
- The current TVL, fee-analysis and profitability behavior and their existing data sources, plus unrelated local work.

## Existing fee-stack and profitability features

These are existing Powerglove features, not the three historical fee/earnings views being migrated:

| Request | Visible feature | Additional data needed |
| --- | --- | --- |
| `GET /api/fees/stack` | **Fee Analysis**: nested vault/strategy tree, effective fee rates, fee capture and trend information. | Current allocation relationships, debt/capital values and fee configuration. Captured fees and trends also join the core/profitability responses in the UI. |
| `GET /api/profitability` | **TVL vs Fee Yield** scatter plot, classifications and profitability trends. | Fee history and suitable TVL/period inputs for yield and trend calculations. |

Yearn-data's existing historical exports do not implement these endpoints. The core migration should preserve their current source and separate routing where needed. Their reimplementation is a later scope; removal or disabling is not assumed.

## Expected change boundary

| Component | Necessary work | Boundary |
| --- | --- | --- |
| yearn-data projections/serving | Select completed paired runs; derive the three JSON views and filters; retain quality/provenance context internally. | Reuse existing results/functions. A thin read-only adapter is the recommended implementation; no duplicate fee engine or new ingestion pipeline. |
| yearn-data publication/operator | Explicit dataset selection, coherence checks, repeatable manual refresh, complete-dataset activation. | No scheduler, automatic broad backfill or new operational control plane. Serving framework and hosting provider remain implementation choices. |
| Powerglove fee types/helpers | Adapt to the display contract and calculate charts from published amounts. | Do not reproduce accounting or historical overlap logic in React. |
| Powerglove Fees panel/routing | Connect core views; display published amounts; retain normal request handling and existing analytics routes. Remove public developer-quality indicators if present. | No missing-data badges, coverage counters or mandatory cutoff displays; keep existing analytics and TVL behavior. |
| Validation/handoff | Focused accounting/contract/UI checks and actual-source preview with exact source/run provenance. | Do not require live RPC or upstream price repair for offline acceptance fixtures. |

Recommendations are not fixed technology requirements. In particular, a new database schema, JSON export format, web framework, schema version bump or separate service deployment needs a concrete reason; none is required merely to resemble the reference service.

## Acceptance checks

| Check | Observable result | Requirements |
| --- | --- | --- |
| A1. Dataset selection | Incomplete/wrong-analysis runs and incompatible cutoffs/policies are rejected. Developer publication records identify the runs used by all three views; an interrupted refresh retains the previous selection. | R1, R8 |
| A2. Filter reconciliation | Summary, sorted monthly buckets and per-vault rows match the same pinned event/report cohort. Inclusive start, exclusive end, current UTC day, chain isolation and 30D/90D/1Y/all-time behavior are exercised. History and headline ranges retain their documented differences. | R2 |
| A3. Fee identity | A nested strategy charge of $10 and allocator charge of $9 produce $19 gross charges in that defined scope, with a family breakdown. Paired evidence, components and distributions do not add additional charges; equal distinct charges remain distinct. | R3 |
| A4. Earnings independence | Active and retired V2/V3 reports appear together; retired incident adjustments apply. A priced report with unresolved fee evidence contributes earnings while its fee remains unavailable. | R4 |
| A5. Layered recognition | The $100 strategy gain and $90 allocator recognition are identifiable reported values and are not labeled $190 consolidated yield. Tokenized P&L is not silently added to main earnings or replaced with zero. | R4 |
| A6. Developer diagnostics | Yearn-data retains missing accounting, metadata, prices, reasons, affected counts and repair targets. These remain distinct from proven zero and independently known earnings. Its published arithmetic uses available accepted values; unavailable source amounts are never invented. | R5 |
| A7. Simple rendering | Published summary/month/vault amounts render normally despite internally recorded gaps. No public completeness badges, gap counters, source-failure explanations, cutoff notices or partial-state propagation are required. A wholly absent display value uses ordinary missing-value behavior, not a fabricated zero. | R5, R6 |
| A8. Requests and errors | Initial loading, ordinary HTTP failure/retry, empty results and filter changes render correctly. Older responses/caches cannot overwrite or mislabel the selected scope. Source-quality reasons stay in yearn-data. | R6 |
| A9. Existing analytics | Core views use yearn-data while fee-stack/profitability and TVL requests continue reaching their intended existing providers. The origin change does not silently remove those features or route them to unsupported endpoints. | R7 |
| A10. Actual pairing | Build Powerglove and inspect the published cards/charts/vault rows against the new adapter, including datasets with internally known gaps. Verify local proxy paths, production routing configuration, preserved analytics, source revision and developer run context. | R7, R8 |

Reuse existing accounting, retired-history, cutoff and export tests. Add only tests needed to protect the new selection/filter/contract/rendering behavior. Run the applicable repository checks when implementation changes land. This requirements-document edit does not claim that those future acceptance checks have passed.

## Non-goals and scope expansion

The first delivery excludes complete historical discovery, broad event/price backfill, recovery of every accounting deferral, upstream yearn-prices fixes, global fallback-policy changes, treasury attribution, historical yield consolidation, TVL migration, reimplementation of fee-stack/profitability calculations, new chart families, a general strategy catalog, automated scheduling and production deployment. Developer diagnostics remain in yearn-data; building or extending a public data-quality dashboard is also excluded.

Historical yield consolidation is a consequential follow-up, not a display correction. The inspected TVL reference preserves parent/strategy/child identity and historical ownership, including V2-to-V3 routers. Those patterns inform dated earnings attribution. Today's allocation graph or the TVL stock-subtraction formula cannot establish period-specific earnings overlap.

If consolidated yield becomes necessary for the requested headline, explain the overlapping reports and supported history, define gross underlying versus vault-recognized versus depositor-net earnings, and agree the smallest attribution scope before adding historical relationship acquisition/reconstruction. Until then, retain the reported-P&L definition and consolidation limitation in yearn-data. Powerglove does not need a warning about each missing observation; the backend must still avoid declaring an unverified consolidated methodology.

Optional improvements remain excluded unless tied to an observable requirement or a demonstrated regression. If native outputs cannot support a required filter or completeness field, identify the concrete gap and extend that projection only; do not silently expand into a replacement pipeline.

## Implementation sequence and completion

1. Finalize the coordinated JSON shape and selected-dataset context for the three core views. Record inclusion and pricing policies, and reconcile shared price evidence.
2. Implement the read-only adapter and manual publication using completed results. Validate filtering, fee identity and earnings independence, retaining source-quality diagnostics in yearn-data.
3. Adapt the Powerglove fee types, helpers and panel to published values. Retain TVL and existing fee-stack/profitability source routing; remove public developer-quality displays from the pairing contract.
4. Reconcile representative output against the canonical snapshots and review the actual paired preview. Document source revisions, run IDs, policy, cutoff, limitations and refresh commands.

The first pairing is complete when A1–A10 pass for the agreed scope and the consumer preview demonstrates the new source with existing analytics preserved. A standalone report, successful export or service response alone is not completion. Missing historical prices/evidence may remain internally tracked while available amounts are served. Developer records must describe the actual scope and retain a concrete route to investigating and repairing the gaps.

## Source references

- Implementation and manual refresh: [Powerglove pairing operator guide](powerglove-pairing.md).
- yearn-data: `src/yearn_data/analysis.py` (`run_lifetime_yield`, cutoff and incident handling); `fee_valuation.py` (accepted fee rows, Yearn EOD policy, family/completeness outputs); `fees.py` (charge binding/acceptance); `exports.py` (completed-run selection and context).
- Maintained operators: [earnings and fees](earnings-and-fees.md), [earnings pricing](earnings-pricing.md), [fee accounting](fee-accounting.md).
- Powerglove at the verified commit: `src/components/landing/native-stats/FeesPanel.tsx`, `canonical-fees.ts`, `fee-history.ts`, `hooks.tsx`, and `vite.config.ts` in `/home/dev/raaaws/worktrees/yearn-powerglove-qtov`.
- TVL reference and its verification limits: [accounting contract](powerglove-accounting-contract.md#what-to-reuse-from-yearn-tvl-service). Reference source is local code, not proof of a deployed revision or complete historical relationships.
