# yETH recovery distributions

The Ethereum Yield Vault `0xd7a540ba3626c0aa66e7db4088971d0cd64695b6`
uses the Recovery Vault `0xe5387cd454dcc542421c069c009d915ab9efaafd` as its
accountant. The accountant returns the smaller of the report gain and the
remaining recovery target as the fee operand. Once the target is satisfied,
it returns zero. These deductions fund creditor recovery, not manager revenue.

Source: [Sourcify exact-match recovery contract](https://sourcify.dev/server/v2/contract/1/0xE5387cd454Dcc542421c069C009D915Ab9EFaaFd?fields=all),
`src/Strategy.sol:yETHRecoveryVault`, deployed at block 24522098.
Verified runtime hash: `0x48ca6275895571ea285bae62f082392412ccc3ac035877c881a05ade9cf0653e`.

## Accounting

- Preserve raw StrategyReported deductions as `reported_total_fees_raw`.
- Classify the deduction less actual protocol fees as `recovery_distribution_raw`;
  expose its valuation as `recoveryDistributionUsd`.
- Actual protocol fees remain fee revenue. Do not presume a perpetual 100% split.
- Require receipt identity, report amounts, the historical accountant, its yield
  vault, and its exact runtime hash to match before reclassification. Retain
  evidence for offline replay. Unknown configurations remain unresolved.
- Index the Recovery Vault as a Tokenized Strategy and retain its observed fees,
  including any future nonzero charges. Its 21 reports through the publication
  cutoff charged zero protocol and performance fees.
- `scope=product` groups both contracts under **yETH Recovery**. Fees include both
  members; investment earnings come from the Yield Vault only. Contract scope
  remains available and does not relabel raw records.

Recovery reports can recognize both forwarded investment yield and donations.
Neither is additional underlying investment earnings. Normal deposits are capital;
yield subsequently earned on that capital is investment earnings. Direct donations
are recovery contributions, not fee revenue or additional product investment yield.
This change excludes the receiving leg's gains from product earnings; it does not
invent a separate donation dollar amount. Treasury/donor transfer reconciliation
is outside the fee correction and remains unquantified.

## Verification and publication

46 historical Yield Vault reports were verified at their receipt block hashes.
The existing publication incorrectly classified $110,247.8568671142692748193912566783963
of recovery distributions as fees. Tests cover receipt/configuration rejection,
real protocol fees, zero/partial distributions at the target, offline replay,
USD/API fields, and product earnings deduplication.

The correction retains the existing October 9, 2026 exclusive cutoff and earnings
run 15. Publication compares every prior fee event, requires all unrelated values
to remain unchanged, and admits only the 21 Recovery Vault events as new records.
Raw reports and the previous selected release are retained for recovery.

Initial correction fee run: **22** (earnings run **15** unchanged).
Release: `cf89bf467280128d4668d7c51038ddee9324aac47b2d7018bef7d83f412f1e42`.
Financial dataset: `50505ec4da8fcd4cff03ab0eef3221df6c3f2b5d2610848a55e38b6d30316d04`.
Reconciliation: 117,339 existing events unchanged, 46 reclassified, 21 added.
The deployed personal preview API confirms zero contract and product fees, with
$110,247.8568671142692748193912566783963 separately exposed as recovery distributions.
