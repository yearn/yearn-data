# yvUSD product accounting

The `scope=product` fee API names the product **yvUSD** and groups these Ethereum contracts:

- Unlocked yvUSD: `0x696d02db93291651ed510704c9b286841d506987` (product address).
- Locked yvUSD: `0xaaafea48472f77563961cdb53291dedfb46f9040`.
- Earlier locked yvUSD: `0xab9018a699003a777d690c156045dfc4a7ef3a96`.

All members' actual fees count toward the product. Earnings come from the underlying
unlocked vault: they already include returns on locked holders' capital. The locked
contracts hold yvUSD shares, and their additional reported gains include locker-bonus
redistributions. Summing their gains again would double count product earnings.
The locker bonus remains separate from actual fees. Contract-scope records and names
are preserved; product charts use the API's `yvUSD` name instead of `USD yVault`.

Both locked contracts are retained in the maintained Tokenized Strategy inventory,
so bounded updater runs collect their fee reports, including any future actual fees.
The earlier contract's verified deployment is block 24271898; the current contract's
is block 24329199. Both expose API version 3.0.4 and use yvUSD as their asset.
Sources are Sourcify exact-match `src/LockedyvUSD.sol:LockedyvUSD` at each address.

The user selected full onchain history rather than a public-launch cutoff. Preserve
the four underlying reports with real fees on January 26–28, 2026 (UTC):
$0.533052320825365730302 management and $0.9690390576925535207695 performance fees.

At the unchanged October 9, 2026 exclusive publication cutoff:

- Product earnings: $284909.3705837046106889831863.
- Actual fees: $1.5020913785179192510715.
- Locker bonus: $30208.3417791514335400091614, already included in underlying gains.
- Current locker: 67 onchain reports; earlier locker: 1. All charge zero additional fees.
- The backfill adds two missing current-locker reports and the earlier locker's report.

Publication validation requires every previously published fee event to remain
identical and only those three zero-fee reports to be added. Earnings run 15 and the
existing TVL dataset remain unchanged; the previous release is retained for rollback.

Selected fee run: **23**.
Release: `dfa8a0e06ba88a785c5c3781327c727282276eb94e23a20ee55ffe3f290a1ff9`.
Financial dataset: `1721b6243bdd28fe16c8e7dd9c2bbdc12f94c5f4e17e24535a46831b07eb9d30`.
Reconciliation confirmed 117,406 unchanged prior events and three added zero-fee reports.
