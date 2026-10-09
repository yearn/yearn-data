# One canonical historical dataset

The database retains the best accepted record for every historical identity.
Old dashboard editions are not a retention requirement. Daily history, exact
amounts, source evidence, and the distinction between zero and unavailable data
are requirements.

## Consolidate existing data

First publish a coherent hosted release with prepared fees, TVL history/totals
and matching analytics. The selected release determines the canonical values.
Then preview consolidation:

```bash
PYTHONPATH=src python -m yearn_data.cli --db neon consolidate-history \
  --receipt artifacts/canonical-plan.json
```

The default writes only the optional report file. Temporary database tables are
rolled back. It refuses running jobs, unpublished finished TVL observations, and
any analysis selection that would remove a historical event identity.

For the first migration, make a current database backup and prove restoration in
an isolated database before applying. The older SQLite archive does not include
subsequent Neon updates. Coordinate writers, then apply:

```bash
PYTHONPATH=src python -m yearn_data.cli --db neon consolidate-history --apply \
  --receipt artifacts/canonical-applied.json
```

The transaction locks affected tables against other writers while allowing
ordinary reads. A lock conflict times out instead of waiting indefinitely.
Consolidation rolls back if any preservation check fails.

## What survives

- Each `(chain_id, vault, timestamp)` snapshot and each
  `(chain_id, parent, strategy, child, timestamp)` position retains the observation
  selected by the existing API: known values precede unavailable retries, then
  newer observations win at the same quality. Published observations take
  precedence over records from failed runs.
- Keys found only in failed runs also survive, retaining their failed provenance.
  This does not publish previously unpublished observations.
- The exact JSON, source run, snapshot block, and every historical identity are
  checked against a temporary inventory before commit. Run IDs are provenance;
  they are not a reason to discard earlier backfill dates.
- Selected earnings/fee outputs retain their row order and bytes. One completed
  result per other analysis type is retained, including raw canonical-fee exports.
  Every historical event identity in older outputs must exist in the retained
  output of that analysis type and output name.
- Current chart rows, totals, prepared financial rows and publication metadata
  are checked by key and SHA-256 of all fields. They remain unchanged.
- Source events, fee receipts, traces, prices, discovery evidence, coverage,
  checkpoints and small run metadata are preserved.

Superseded TVL observations, older derived caches, older analysis outputs and old
hosted publications are removed. Old completed analysis metadata is marked
`superseded`; its IDs are not reused. A small `canonical_history_receipts` row
records the selected revision and all preservation counts.

## Ongoing updates

`publish-hosted` now consolidates after validating and selecting the new coherent
release. `scripts/update_postgres.py` also publishes that hosted release, so its
successful updates finish with canonical retention. Preparation may temporarily
hold both the old and new results; after consolidation only current serving data
remains. A failed consolidation leaves both revisions available for inspection
and returns an error; the already validated selected release remains usable.

Publication IDs remain cache/revision tokens. The hosted API returns HTTP 410 for
a request pinned to a superseded ID, with the current release ID. Consumers should
reload `/api/publication` and retry the related views together. They must not
combine values from different revisions. Unpublished collection/preparation
commands do not retire serving data on their own.

Use the updated hosted API with the updated publisher: its obsolete-pin and
in-flight revision checks are part of this retention contract. Applying the
database consolidation alone does not deploy those API changes.

Older local manifest files may remain as small operational records. Their old
results are no longer supported; use the current published pointers. Do not
reselect an obsolete release or treat removed historical editions as backups.

## Storage reclamation and validation

Deletion first makes space reusable inside Postgres. Measure actual table/index
sizes after a separate planned rewrite/compaction; do not equate deleted rows
with immediately reclaimed disk. Rewrites can require additional disk and an
exclusive lock, so coordinate them separately from the verification transaction.

Required validation includes all-key preservation counts, exact selected values,
current dataset IDs, historical and per-vault API responses, nested accounting,
and a second consolidation with no further rows removed. A matching headline
total alone is insufficient. Regression tests inject corruption to prove that a
verification failure rolls back the whole transaction.

Observation reads order by the complete historical identity. Physical rewrites
must not change vault/strategy collection ordering through unspecified SQL ties.
