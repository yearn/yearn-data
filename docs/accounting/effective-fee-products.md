# Current fee entries for yBOLD and yvUSD

The Effective Fees table distinguishes a verified fee at the selected vault from
an unavailable combined rate across all of its underlying allocations. A partial
allocation history must not erase a known contract fee. `feeAvailability` tracks
own-fee support independently of the stack's `availability`.

## Source and display rules

- Staked yBOLD (`0x23346b04a7f55b8760e5860aa5a77383d63491cd`) is named **yBOLD**
  in fee views. Its onchain `performanceFee()` returns 1000 basis points (10%).
- The base vault (`0x9f4330700a36b29952869fac9b33f45eedd8a3d8`) is named
  **Unstaked yBOLD**. Verify its accountant's runtime hash and staking recipient
  before treating the accountant's routing as a distribution rather than a fee.
- **yvUSD** combines the currently visible unlocked and locked entries. Add their
  **external TVL**, which already deducts upstream ownership. Do not add the
  locked vault's gross TVL to the unlocked vault's gross TVL.
- The yvUSD accountant exposes `feeConfig()`, not the standard Accountant's
  `getVaultConfig(address)`. Verify its runtime and underlying asset before
  reading management and performance fees. The locker bonus is a distribution,
  not an additional fee.
- At the user's request, the combined yvUSD row displays its own product fee;
  underlying fees remain in the expanded breakdown. The current rates are zero
  at both product components. Unknown or future nonzero grouped configurations
  remain unavailable until their combined rate has an explicit definition.

`/api/fees/stack` retains raw `chains` and their calculated rates. Its canonical
`entries` provide names, component roots, adjusted TVL and `feeRateBasis` (`stack`,
`vault` or `product`). Powerglove uses those entries directly, with no product
address rules. Both yvUSD roots remain available to inspect; their allocated
capital is not additive across layers. Historical product earnings and fees use
the existing `scope=product` rules and are unchanged by this display policy.

## Verification and publication

Finalized Ethereum block **26178806** confirms a 10% staked yBOLD fee and zero own
fees for unstaked yBOLD and both current yvUSD components. yvUSD's `lockerBonus`
is 1000 basis points and stays separate from fees.

The new analytics publication retains all **1021 unrelated configurations** and
preserves profitability, comparison and membership payloads. It changes only the
four verified product configurations and the current fee view. Fee run 23,
earnings run 15, financial history, the TVL dataset and the cutoff are retained.

- Release: `55038855420476cccdfb745d808cb966ef7859c59c90f3d489c9d0c0a23706ae`.
- Analytics: `0016791df53858c39757eb9dc917f0f5ad7444139626f97eeca2d4fff8657b4c`.
- Previous release: `dfa8a0e06ba88a785c5c3781327c727282276eb94e23a20ee55ffe3f290a1ff9`.
- yBOLD external TVL: $6,806,710.843030453; Unstaked yBOLD: $74,127.87559473967.
- yvUSD combined external TVL: $11,989,399.785465341.

The hosted API serves immutable analytics bodies from Neon; it needs no code
deployment for these additive fee-view fields. Prior publications remain intact
for rollback. Tests cover incomplete allocations, genuine zero fees, grouped
external TVL, unknown recipients/code, changing product rates and locker bonuses.

## Carry-forward onto the newer TVL publication

A concurrent TVL publication used the previous analytics method and omitted
canonical fee entries. Carry the corrected entries forward without rolling back
that publication's TVL work or changing its fee configurations and other analytics.

The resulting selected release is
`a3878f2f580a44587e2b95aaf23c9440374593341e29f76f2482e0460f549681`, with analytics
`c316554dad535ea7e6702a9726be0a88f39550280da228b6660cfbcfde79cd4a` and the newer TVL
dataset `9f5d1262367eb80c55793f6041c7973d9ecdee383b0d5ae7fb665c48e1de1f57`.
The financial dataset is unchanged, as are the product fee rates and combined
yvUSD external TVL. The previous TVL release
`b04af26bf93ac8065d439063d2c788abb9274f3728b80d2ba0b6faba8c32c113` is retained.

Future publication jobs must use `powerglove-analytics-4` from this change so
refreshing the TVL dataset also retains the canonical fee entries.
