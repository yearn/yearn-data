# Earnings and fee accounting for Powerglove

Updated October 5, 2026. This is the integration boundary, not an implemented API or a claim of consolidated historical yield.

For the first pairing's observable behavior, scope and acceptance checks, see the [pairing requirements](powerglove-pairing-requirements.md). Yearn-data retains completeness, reasons, provenance and reporting cutoffs for developer diagnosis and repair. Powerglove receives the published financial values; public gap counters, quality warnings and cutoff notices are not required.

## Historical earnings scope

`analyze lifetime-yield` includes all stored `strategy_reports`, regardless of a vault's current `active` flag. Use both active and retired history; `active=0` alone would discard active-vault history. Missing vault metadata must not erase a stored report. Raw report data stays unchanged, known incident adjustments apply to analysis outputs, and unavailable prices remain visible in report counts and event values.

Earnings do not require accepted fee accounting. A report's gain/loss can be known while its fee mint still lacks execution evidence. Price, cutoff and discovery coverage must be reported separately from fee acceptance. The existing lifetime-yield price policy can use a DefiLlama fallback; the fee USD policy is Yearn-only. A paired API must declare and align its chosen policy rather than treating the two caches as interchangeable.

The lifetime-yield export is currently an incident-adjusted sum of stored report P&L. It is not yet a consolidation of earnings across nested contracts. Including retired reports fixes historical membership; it does not fix layered recognition.

## Different aggregation rules

| Metric | Aggregation rule | Required boundary |
| --- | --- | --- |
| Reported earnings | Preserve gain/loss recognition at each reporting contract. | V2 vault, V3 allocator and Tokenized Strategy remain identifiable. |
| Consolidated economic yield | Count underlying performance once within the chosen universe. | Establish historical parent/strategy/child ownership and recognition; unknown overlap remains an explicit limitation. |
| Gross fees charged across the stack | Sum distinct accepted charges at every included layer. | Each charge is counted once; carry family and accounting/pricing completeness. |
| Protocol/treasury revenue | Attribute the relevant receipts to the defined recipient universe. | Gross depositor-paid fees are not a substitute for revenue attribution. |
| TVL | Count capital once within the chosen universe. | Deduct only the internal exposure covered by that metric. |

For example, suppose a strategy earns $100 and charges $10. An allocator holding that position recognizes the remaining $90 and charges $9. Gross fees across those two layers are $19, assuming both charges occurred and were accepted. The two reported gains are $100 and $90; adding them gives $190 of report recognition, not $190 of underlying economic yield. Depositor net earnings in this simplified example are $81. Protocol-wide gross yield, vault-reported gain, and depositor net yield are different metrics.

Deduplicate fee representations by their bound charge/event identity (chain, transaction, log/report binding), not by nesting or matching amount. A report and its paired fee evidence can describe one charge. Components are not extra charges: V3 allocator protocol fees are already included in total fees, while Tokenized total fees include protocol and performance components. Recipient distributions substantiate or move an existing charge and must not add it again. Missing or conditional fees never become zero.

The exports continue to retain separate family rollups. A combined gross-fee view is a consumer projection with an explicit inclusion scope; it does not change existing accounting tables or formulas. Yield consolidation must also handle nesting within the vault universe, such as V2 routers into V3 vaults; merely excluding Tokenized Strategy earnings is insufficient.

## What to reuse from yearn-tvl-service

Inspected local `yearn-tvl-service` branch `codex/yearn-price-prod-benchmark` at `fb1b561579e7e4855916c81726dbb81ed48dee0f`, read-only. This is a code reference, not verification of the service's deployed revision or live coverage.

- `packages/api/src/services/tvl.ts`: `computeOverlap()` detects direct strategy-to-vault allocations and explicit intermediary mappings. The flow graph retains parent/strategy/child relationships and provenance, including retired V2 router paths.
- `packages/shared/src/strategy-overlaps.ts`: direct, intermediary, V2-to-V3 router and cross-chain mappings demonstrate that relationships are not a simple V2-versus-V3 split. Some current V2 router values are explicitly source-vault-TVL estimates.
- `packages/api/src/services/tvl-history.ts`: `computeOwnershipPoints()` uses historical block-specific `balanceOf(holder)` and `totalSupply()` values; `aggregateExternalTvlHistory()` deducts owned child TVL at each timestamp.
- `packages/shared/src/curation-products.ts`: defines the included product universe and exposes gross TVL, known internal overlap, resulting TVL, and unresolved mapping limitations.

Reuse the relationship identities, dated evidence, explicit universe and gross/overlap/result distinction. Do not copy the TVL subtraction formula into yield accounting: TVL is a stock at a timestamp; earnings are flows recognized over periods. Today's allocation or a retirement/migration mapping cannot establish historical earnings overlap. A current missing archive observation is not zero historical ownership.

Before claiming consolidated yield, define the accounting boundary (gross underlying yield, vault-recognized P&L, or depositor net), historical ownership intervals, fees retained downstream, reporting delays, losses/recoveries, and asset/USD valuation policy. Keep unresolved or estimated attribution distinct from verified adjustments. Preserve the original reports and adjustment evidence so the resulting subtotal can be audited.

## Powerglove handoff

Powerglove `codex/qtov-on-improved-fee-data@04a1a30586d17a3b3da86ccdd30ecf2560a2435a` requests core fee summary, monthly history and vault rows, alongside optional fee-stack/profitability analytics.

1. Expose core JSON views from a pinned completed snapshot, using consistent time/chain/family filters and cutoff. Include active and retired history. Retain snapshot identity and independent accounting, pricing and discovery diagnostics in yearn-data publication records.
2. Define earnings as incident-adjusted reported P&L until historical consolidation is supported. Keep that methodology in yearn-data and keep per-family/per-vault financial views available. Gross fees across layers include each distinct accepted charge once.
3. Preserve raw and incident-adjusted earnings and independent known/unknown counts in yearn-data. Do not derive report P&L exclusively from accepted fee projections. The display API can serve available aggregate amounts while developer records retain the missing-source diagnostics.
4. Preserve the existing fee-stack/profitability and TVL features on their current sources during the core migration, separating routing as needed. Rebuilding those analytics in yearn-data is later work. TVL relationships can inform historical earnings attribution.

## Acceptance checks

- Active and retired V2/V3 reports appear together in lifetime exports and rollups; retiring a vault does not remove earlier P&L. Retired incident reports still use the existing adjustments. Unpriced retired reports remain counted with null event USD values.
- Separate nested strategy and allocator charges both contribute once to the defined gross-fees metric; paired fee evidence, components and distributions do not create additional charges.
- Nested $100/$90 gains are retained as reported values but not labeled as $190 consolidated economic yield. V2-to-V3 nesting, partial ownership and delayed parent recognition are represented in the consolidation specification.
- Missing relationship/history evidence cannot certify consolidated yield. Yearn-data retains snapshot cutoff, pricing/discovery diagnostics and repair targets. Powerglove can render the available amounts without public diagnostic counters or warnings.

Base branch `ross/pr-08-fantom-replay@5fcea03f2803d6d4fed761aaea1d03ddb9d16965` already includes all stored reports and supports the closed-day earnings cutoff. The continuation branch `ross/powerglove-accounting` adds accounting documentation and regression coverage without reapplying the older active-filter patch. JSON serving, shared paired valuation and historical yield consolidation remain follow-up work.
