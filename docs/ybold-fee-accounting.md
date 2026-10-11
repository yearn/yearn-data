# yBOLD fee classification

The Ethereum yBOLD allocator (`0x9F4330700a36B29952869fac9b33f45EEdd8A3d8`)
uses the V3 fee mechanism to allocate yield to staked yBOLD. These allocations
are depositor yield, not manager revenue. Underlying strategy fees remain separate.

The verified accountant is `0x53acEBB9470Cfc9D231075154f5dcF1586A4c6fa`.
Its `report()` returns the gain after any loss restoration as `totalFees`;
`distribute()` transfers the accumulated tokens to its configured recipient.
The verified staking recipient is `0x23346B04a7f55b8760E5860AA5A77383D63491cD`.

Source evidence: [deployed accountant verification](https://sourcify.dev/server/v2/contract/1/0x53acEBB9470Cfc9D231075154f5dcF1586A4c6fa?fields=all).
The exact-match runtime hash is
`0xa3098fa22faa347d2cded2cac45f884a69895472d3af03f2d369a4247d477e90`.

## Accounting rule

`canonical-fees-8` requires a successful receipt matching the report transaction,
block, log index, strategy and amounts. Historical `accountant()`, `feeRecipient()`
and runtime code are read at the receipt's block hash. Unknown implementations,
recipients or unreconciled amounts remain unresolved instead of falling back to
counting the entire deduction as revenue.

- `reported_total_fees_raw` / `reportedTotalFeesUsd` preserve the raw deduction.
- `staker_distribution_raw` / `stakerDistributionUsd` contain the deduction less
  genuine protocol fees. This measures allocation to the staking recipient, not
  a separate timestamped payment or a new earnings event.
- `total_fees_paid_raw` / `totalFeesPaidUsd` retain genuine protocol fees.
- Manager, management and performance revenue at this allocator layer are zero
  under this verified implementation. This does not zero fees on its strategies.

Reported gains, losses, prices, incident adjustments, and the earnings publication
are unchanged. The chart consumes the corrected fee metric without a vault exclusion.

## Refresh and publication

Normal `index-fees --canonical --version v3` upgrades stale yBOLD rows and collects
evidence for new reports. `recompute-fees formulas --version v3` replays retained
evidence without RPC calls. Regenerate `fee-usd` with the existing financial
cutoff and deferred-event manifest, validate `select-pairing`, and use
`publish-hosted --prepare-analytics` to prepare and select a coherent release.
Keep the TVL publication and earnings run unchanged for a fee-only correction.
Do not edit completed analysis outputs or reuse their dataset IDs.

Receipt fixtures cover zero deductions and positive distributions. Tests also
cover actual protocol fees, rejected evidence, stale-row upgrades, offline replay,
USD valuation, and API aggregation. Full historical evidence and original canonical
rows are retained locally under ignored `artifacts/ybold-fees/` for reconciliation.

## October 11, 2026 reconciliation

All 1,216 stored allocator reports through the existing October 9 UTC cutoff
passed the historical implementation and recipient checks. Their deductions total
624,774.696458441596357891 BOLD, with zero protocol fees at this allocator layer.

Fee run **20**, paired with unchanged earnings run **15**, reclassifies
**624567.5167376424520436828426798382367545 USD** from fee revenue to staker
distributions. Every existing field on all **116,166 non-yBOLD fee events** matches
run 18. yBOLD gains, losses and prices also match; no additional earnings are created.
The new financial dataset is
`cc99f51befd3174ccf6a691a5e807e12b8bf298e807b2089007811f82cc5fbf5`.

The prior selected release is retained for rollback:
`3c59947823e61cfa85b6f4ffd45c7762ef878363f6154a89857d813fed9a6dae`.
Use `select-hosted-release` with that ID to restore the previous published view.

Selected hosted release:
`5a7b195dca6e2561fa0d8437e15a6ae78db545c8b590b666c7d930af12dec319`.
The running Powerglove proxy was verified to return this financial dataset and
zero yBOLD allocator fees. Existing API code reads the corrected fee total
immediately; exposing the additional `stakerDistributionUsd` response field
requires deploying this branch's API code.

Automatic post-publication history consolidation was stopped before deletion
for this rollout. Both releases and all 122,825 output rows of each fee run
(18 and 20) were verified retained. A later consolidation can retire the rollback
publication; check retention before using the rollback command.
