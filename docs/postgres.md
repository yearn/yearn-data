# Postgres storage and Neon migration

The same indexing, accounting and API code can use SQLite or Postgres. TVL keeps
its `tvl_*` tables; fee/earnings tables retain their names. Calculations, stored
history and unavailable-versus-zero semantics do not change.

## Select a database

Put the Neon connection URL in the ignored `.env` file as `NEON_DB_URL`. Select
Postgres explicitly with `--db neon`; adding the variable alone does not switch
existing SQLite jobs. The pooled Neon URL works for ordinary jobs and APIs.
Imports and monthly backfills use the corresponding direct endpoint automatically.

```bash
yearn-data --env /path/to/.env --db neon init-db
yearn-data --env /path/to/.env --db neon tvl discover
yearn-data --env /path/to/.env --db neon analyze lifetime-yield
```

Use `init-db` for a fresh installation. **For an existing-history migration, run
the import first:** it deliberately refuses a schema with existing tables.

The default Postgres schema is `public`. `YEARN_DATA_DB_SCHEMA` selects another
already-created schema, principally for isolated testing. Credentials stay in the
environment; publications and status files contain the logical name `neon`, never
the connection URL. Loading a Neon publication requires the same environment and
schema configuration used to publish it.

## Import the shared SQLite history

Confirm Neon has storage for the existing database plus indexes and growth.
SQLite file size is a planning reference, not the resulting Postgres size.
Pause SQLite writers before the final import; read-only consumers can stay up.

```bash
yearn-data --env /path/to/.env --db neon migrate-postgres \
  --source /path/to/yearn-shared.sqlite --check-only

yearn-data --env /path/to/.env --db neon migrate-postgres \
  --source /path/to/yearn-shared.sqlite \
  --receipt /path/to/migration-receipt.json
```

The importer streams a consistent, read-only SQLite snapshot via Postgres `COPY`;
it creates no dump or second full SQLite file. All source tables and explicit
indexes are copied, including superseded runs, coverage, repair checkpoints and
source evidence. Tables are ordered by foreign-key dependencies. The import is
one transaction: an interruption rolls back its tables and data, and the unchanged
SQLite source remains available for retry or rollback.

SQLite `INTEGER` becomes `bigint`; `REAL` becomes `double precision` to preserve
existing floating-point precision. Exact raw token quantities, Decimal strings
and JSON remain text. Existing run IDs and reserved sequence values survive.
`analysis_outputs` gets an explicit `rowid` identity column whose imported values
preserve SQLite output ordering. Future output IDs start above the imported maximum.
Postgres indexes cover selected analysis outputs and TVL dates across multiple
runs, so a current-date API lookup does not scan the entire backfill.

Before commit, the importer checks every destination table count and every stored
analysis output fingerprint. Postgres enforces the copied foreign keys and other
constraints. This verifies storage without rerunning the historical backfill or
requiring another comparison against the old TVL service.

## Publish and serve from Neon

Use the same completed analysis run IDs to create a **separate** Neon publication.
Fee/earnings dataset IDs stay the same when the selected output text/order is
unchanged. TVL publication IDs include the database reference and are republished
for Neon. Existing SQLite publication files are not changed automatically.

```bash
yearn-data --env /path/to/.env --db neon select-pairing \
  --earnings-run-id 13 --fees-run-id 12 --out data/powerglove-neon

yearn-data --env /path/to/.env serve-pairing \
  --publication data/powerglove-neon --tvl-publication data/tvl-neon \
  --host 127.0.0.1 --port 3491
```

The run IDs above are examples for the current local dataset; select the intended
completed pair for another installation. Validate the new API before switching
consumers. Keep SQLite and its rollback archives until the Neon deployment is
accepted. Writers need `--db neon` as well as the correct environment.

The monthly TVL backfill also accepts `--db neon`. It takes a Postgres advisory
lock through its direct connection so another host cannot run the same backfill
concurrently. The regular `tvl collect` command retains its existing scheduling
behavior; coordinate it with the monthly backfill.

## Validation

```bash
python -m pytest -q
YEARN_TEST_POSTGRES_URL='<disposable Postgres connection URL>' \
  python -m pytest tests/test_postgres.py -q
```

Integration tests create and remove uniquely named schemas. They cover atomic
coverage writes, interrupted import recovery, reserved IDs, exact amounts,
missing values, output order, fee price retries, and existing discovery,
collection, earnings and API regressions against a real Postgres server. They
never use public application tables.

The small transport adapter translates the toolkit's qmark parameters, schema
types and scalar `json_extract` queries. It is not a general SQLite emulator.
SQLite file consolidation and importing the old service's SQLite catalog remain
explicit SQLite operations.

## Prepare chart history after publishing TVL

Run this once for each newly published TVL dataset, before sending chart traffic
through the API:

```bash
yearn-data --env /path/to/.env --db neon tvl prepare-history \
  --publication data/tvl-neon
```

This runs the existing dated accounting in bounded batches and stores slim vault
rows plus daily chain/version totals in `tvl_chart_rows`, `tvl_chart_totals` and
`tvl_chart_publications`. Nested holdings, repairs and unavailable values use the
same calculations as the source history. Preparation commits atomically; an
interruption can be retried. Repeating the command skips completed preparation.
The cache is keyed by immutable dataset ID and accounting version. Bump the cache
version when changing the accounting used to produce these rows.

The API reads prepared values without reconstructing raw snapshots and positions
for each request. It still serves unprepared datasets through the original path,
so **a new publication needs this preparation step to retain fast chart loading**.
This does not schedule ingestion or change which dataset is selected.

Chart clients can request `format=chart` to omit duplicate diagnostic points and
price references. On constant-price vault history, `top=10` returns the ten vaults
with the largest latest known values plus an `All other vaults` series, and one
price-neutral total across every vault. `meta.topSeries` identifies the prepared
series so clients do not reduce them again. Default requests keep the full
response. Responses use gzip when the client accepts it.

## Local operation after cutover

Set `YEARN_DATA_DB=neon` alongside `NEON_DB_URL` in the ignored local `.env`.
Commands then default to Neon; an explicit `--db` always wins. Run commands from
the Postgres checkout rather than an older branch without this configuration.
No automatic ingestion schedule is installed.

`scripts/update_postgres.py` runs a bounded report acquisition, fee calculation,
Yearn-only pricing, paired analyses, one full-catalog TVL close and chart
preparation. Select block bounds and a closed UTC cutoff explicitly. A report
window covers the selected chain; it does not claim all-chain fee freshness.
The publication root contains `fees/` and `tvl/` directories. Preparation uses a
separate pointer before publishing the serving TVL selection. The updater then
publishes a coherent hosted release and performs
[verified canonical-history consolidation](canonical-history.md). Daily historical
coverage is retained; obsolete publication editions are retired.

```bash
python scripts/update_postgres.py --db neon --env /path/to/.env \
  --chain eth --from-block START --to-block END --cutoff UTC_MIDNIGHT \
  --publication-root data/neon-operations \
  --deferred-manifest /path/to/validated-deferred-events.json
```

Coordinate writers: run one update at a time. Pricing and fee-calculation batches
are limited to 500 targets; inspect their results and repeat acquisition/pricing
for larger catch-ups before publishing. Missing values retain existing semantics.

The user service templates in `ops/` serve Neon on localhost 3491 and forward
legacy port 3492 to it without another database reader. The API uses `--published-tvl-only` so acquiring a finished TVL run does not
publish it ahead of chart preparation. They assume the durable
checkout at `~/raaaws/worktrees/yearn-data-neon`.
Copy the unit files to `~/.config/systemd/user`, run `systemctl --user daemon-reload`
and enable the API service and compatibility socket. The Comparison endpoint
still uses the old TVL service's separate SQLite reference database.

## Local Postgres staging

Staging uses a separate Postgres 18 container (matching Neon), database, role and Docker volume.
It binds only `127.0.0.1:55435` and restarts automatically. Its credentials are in
ignored `.env` / `data/local-postgres.env` files. Set `STAGING_DB_URL` to its local
connection URL with `?sslmode=disable`; hosted Neon continues to require TLS.
Select staging explicitly with `--db staging`; its publications store `staging`
rather than a credential-bearing URL. Keep staging publications separate from
Neon publications.

A full staging copy can be seeded with the existing SQLite importer before
retirement. Reimport requires a new empty database; existing tables are never
overwritten. The staging copy is a point-in-time development copy, not a live
replica or Neon recovery backup. Integration tests can use `STAGING_DB_URL` as
`YEARN_TEST_POSTGRES_URL`; they create isolated temporary schemas.

Before removing the shared SQLite source, compress its final stopped-writer
state, fully decompress it, compare SHA-256, and run integrity and foreign-key
checks on the restored file. Keep the verified archive and restore metadata.
Neon recovery settings and a tested recovery path remain a separate requirement.

The existing staging container can be started with
`docker start yearn-data-staging-postgres`. `ops/staging-postgres.compose.yml`
records its image, localhost binding and external volume for future management.
Set `YEARN_STAGING_ENV_FILE` to the existing ignored credential file when using
Compose. Adopting the existing volume requires removing only the stopped
container first; do not remove the volume. Do not run two containers on this port.

The main checkout's virtual environment can use the operational checkout with
`python -m pip install -e /path/to/yearn-data-neon`. Check
`python -c 'import yearn_data.cli; print(yearn_data.cli.__file__)'` before assuming
that an older checkout's command supports the Neon/staging selectors.
