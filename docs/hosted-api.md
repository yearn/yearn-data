# Hosted Powerglove API — local implementation

Branch: `ross/powerglove-hosted-api`, started from `42f98a7`.
The API has a personal Vercel preview deployment backed by a published Neon release.
The existing Powerglove production configuration remains unchanged.

## Boundary

This first implementation adds a Vercel-importable Python handler, a database-backed
publication registry, and CDN cache policy. It preserves the existing fee, TVL,
and analytics response contracts. Collection, pricing, accounting changes, Redis,
research exports and production worker hosting are outside this change.

The existing file-backed server and preview remain available. This implementation
is a foundation, not a declaration that production latency and packaging have been
validated on Vercel.

## Publication flow

The explicit writer command validates the selected financial analysis, requires
prepared TVL rows/totals, and checks that analytics reference those same two datasets.
It stores financial and TVL manifests in `api_manifests`, a coherent set of IDs in
`api_releases`, and atomically updates `api_selection`. Earlier manifests remain
addressable. Conflicting content for an existing identity is rejected.

```bash
PYTHONPATH=src python -m yearn_data.cli --db staging publish-hosted \
  --publication data/staging/fees --tvl-publication data/staging/tvl \
  --analytics-publication-id <prepared-analytics-id>
```

Use the database that actually owns those publications. Source manifests must
reference that same logical database. Existing analytics preparation is a separate
step. The writer uses write-capable credentials; the deployed API should receive
separate read-only credentials. API code only opens read-only connections and never
creates schema, collects data, refreshes prices, or publishes a new selection.

Every request reads the small selected release record. Object caches are keyed by
immutable dataset IDs; selecting another release does not require a restart.
`GET /api/publication` returns the selected release and fee/TVL/analytics IDs.
Existing Powerglove discovery routes and explicitly pinned dataset URLs still work.

## Local entry point

Set `YEARN_DATA_DB` to the logical database name, and supply the matching database
URL in the process environment. The HTTP module does not implicitly load `.env`.

```bash
PYTHONPATH=src python -m yearn_data.hosted_api --port 3494
```

`api/index.py` exports the same handler for Vercel. This uses Vercel's supported
file-based `BaseHTTPRequestHandler` interface and reuses the existing HTTP style
without adding a framework dependency. `vercel.json` routes `/api/*` to that handler.
The bundle exclusions retain runtime inventories but exclude local databases,
environment files, generated artifacts and development files.

Reference: https://vercel.com/docs/functions/runtimes/python/api-directory

## Caching

Successful unpinned responses receive a 30-second Vercel CDN TTL. Explicitly pinned
responses receive a one-hour TTL. The cache key must retain the full query string,
including the publication identity and all filters. Browser responses revalidate;
failed requests use `no-store`. CORS allows public read access without credentials.
A publication change can take up to the unpinned TTL to become visible through the CDN.

Reference: https://vercel.com/docs/caching/cache-control-headers

## Remaining deployment gates

- Measure representative Powerglove requests from fresh processes. Financial reads
  now query compact published financial rows, while current TVL views still
  calculate from stored observations. Broad financial requests still aggregate
  their matching rows in Python; further pre-aggregation may be needed.
- Verify routing, Python version, dependency installation, compressed response sizes,
  function memory/duration and bundle size with a local Vercel build before deploying.
- HTTP gzip compression is implemented; verify its behavior through Vercel and
  response-size limits before switching large histories.
- Exercise the handler against a staged copy of the real publication, compare every
  active Powerglove route, then run a separate frontend preview against it.
- Configure a production read-only database role and choose the deployment region.
- Only after explicit deployment approval: create/configure the Vercel project,
  publish the production release, and change the production Powerglove API URL.

## Acceptance checks for this first pass

Tests remove the local manifest directories before reading through the hosted API,
compare responses with the existing server, check selection changes without restart,
reject mismatched analytics and mutable manifests, verify read-only connections,
and exercise cache/error behavior. PostgreSQL tests use disposable schemas.

## Isolated local staging validation (2026-10-09)

A snapshot of the real selected Neon publications was copied to local PostgreSQL
(`staging`), in the dedicated `powerglove_hosted_20261009` schema. This does not
change Neon or the existing local staging schema. It includes earnings run 15,
corrected fee run 18, all prepared TVL history, current raw TVL observations and
retained analytics inputs. Analytics were regenerated against this coherent selection.
Historical dates now come from prepared chart rows in the hosted reader, avoiding
scans of ingestion history. A regression test checks that history survives removal
of historical raw snapshots.

Local services:

- `yearn-data-hosted-api-staging.service`: same hosted handler, loopback port 3494,
  read-only PostgreSQL account, isolated schema.
- `powerglove-hosted-api-preview.service`: a snapshot of the existing Powerglove
  production build on loopback port 4607, proxying migrated API routes to 3494.
  Frontend source and the existing preview were not modified.

Inspect or restart the API with:

```bash
systemctl --user status yearn-data-hosted-api-staging.service
systemctl --user restart yearn-data-hosted-api-staging.service
curl -fsS http://127.0.0.1:3494/api/publication
```

The private preview URL is provided in the conversation, not committed here.
Test TVL chain/version views and individual chains; then Fees, Vaults & Curation,
Curation Products and Comparison. This is a frozen staging snapshot, not a live
Neon connection. A backend code change needs an API restart. This preview does not
exercise Vercel routing, CDN caching, or serverless cold starts.

Results and screenshots are retained locally under `artifacts/hosted-api-staging/`:

- Eight financial/TVL routes matched the existing Neon-backed API after normalizing
  dataset metadata and array ordering. Browser traversal of TVL, Fees,
  Vaults & Curation and Comparison saw 87 API responses without HTTP failures or
  JavaScript errors.
- First fees summary: 8.0 seconds; subsequent request: 18 ms.
- First weekly all-chain TVL history: 0.39 seconds; subsequent request: 25 ms.
- First price-neutral history: 4.19 seconds; subsequent request: 30 ms.
- Ethereum vault history: 1.36 seconds initially, 0.27 seconds repeated,
  but 14.2 MB of uncompressed JSON.
- Process memory peaked around 1.50 GB after these requests.

These are localhost timings against a local PostgreSQL copy, not production or
Neon latency estimates. Before deployment, replace the financial full-run load
with prepared serving data, validate concurrency, and validate response compression
and payload sizes. The prepared TVL path improves history reads but does not solve
financial cold-start memory costs.


## Compact financial serving follow-up

`publish-hosted` now prepares `api_fee_rows` and `api_fee_datasets` before changing
selection. Preparation uses the validated analysis cohort and stores only the
fields required by consumer calculations, preserving decimal strings, nulls and
source ordering. The hosted reader selects matching rows by time, chain and vault;
it no longer loads or revalidates full analysis outputs on first access. Existing
file-backed serving remains unchanged. Re-publish existing hosted selections once
to prepare these tables; grant the read-only API role SELECT on the new tables.

Financial calculation and validation logic remains shared with the existing API.
Regression cases compare summaries, histories, vault filters and unavailable
amounts even after the original analysis output rows are removed. PostgreSQL
coverage verifies compact row preparation and filtered reads in a disposable schema.

The handler now negotiates gzip for JSON responses over 1 KB, honors `gzip;q=0`,
and emits `Vary: Accept-Encoding`. The tested Ethereum history shrank from
14.17 MB to 3.01 MB over the wire. This does not guarantee every possible query
fits the eventual hosting platform's response limits.

Follow-up local measurements, same snapshot:

- First fees summary: 3.02 seconds (previously 8.00); repeat: 17 ms.
- First weekly fee history: 3.08 seconds; repeat: 23 ms. The old implementation's
  already-loaded first history was 2.26 seconds, so lower cold summary cost is
  accompanied by additional reads for each uncached financial view.
- Sequential benchmark process peak: 541 MB (previously about 1.50 GB).
- A fresh-process browser traversal with concurrent UI requests peaked at 1.15 GB
  and settled around 523 MB. Concurrency and wide financial scans remain deployment
  concerns; this is not a production memory budget guarantee.
- Eight financial/TVL route comparisons still match after metadata/order
  normalization. Browser traversal again completed without HTTP or JavaScript errors.

No Neon publication, existing preview, or Vercel deployment was changed.

## Repeatable Neon publication and rollback

After collectors and analyses finish, select the completed financial runs and TVL
manifest using the existing workflow. Run the following with writer credentials:

```bash
PYTHONPATH=src .venv/bin/python -m yearn_data.cli --db neon publish-hosted \
  --publication data/powerglove-neon \
  --tvl-publication data/powerglove-tvl-neon --prepare-analytics
```

This validates the selected fee/earnings cohort, requires prepared TVL history,
prepares analytics for those exact dataset IDs without changing the legacy
`analytics_selection`, prepares compact financial rows, and atomically selects the
hosted release. Retain the printed release ID in the update job's logs. Failed
preparation leaves the previous hosted selection active; completed intermediate
publications may remain stored and can be reused on retry. Existing collectors
and scheduling are not changed or enabled by this command.

Rollback uses a previously recorded release ID:

```bash
PYTHONPATH=src .venv/bin/python -m yearn_data.cli --db neon select-hosted-release \
  --release-id <previous-release-id>
```

The command checks that referenced prepared datasets and matching analytics exist
before changing selection. API processes read selection on every request; no
restart is needed. CDN TTLs still apply after deployment.

Use separate read-only credentials for the API. The dedicated `powerglove_api_reader`
role has SELECT on serving tables only: `api_manifests`, `api_releases`,
`api_selection`, `api_fee_rows`, `api_fee_datasets`, `analytics_publications`,
`tvl_snapshots`, `tvl_positions`, `tvl_chart_rows`, `tvl_chart_totals`,
`tvl_chart_publications`. It does not receive access to raw analysis outputs or
write privileges. Keep credentials in private environment files, never manifests
or committed documentation. Additional serving tables require explicit grants.

## Neon-backed local validation

A separate API now runs on loopback 3495 (`yearn-data-hosted-api-neon.service`),
using the read-only Neon role above. A frozen Powerglove build runs on loopback
4615 (`powerglove-hosted-neon-preview.service`). The prior staging preview and
legacy APIs remain running unchanged. Private credentials are in the ignored,
mode-600 `data/hosted-api-neon/api.env`; receipts are under
`artifacts/hosted-api-neon/`. No Vercel deployment has been performed.

The initial hosted release is
`63b1d2520ffe771fd3a81c38335305291826eb91cb2686b6a4f7c21b08a1563f`.
Preparation retained the existing legacy analytics selection. Permission checks
confirmed zero writable public tables, no access to raw analysis outputs and no
CREATE privilege on the public schema for the reader.

Eight financial/TVL routes matched the existing API after metadata/order
normalization. Initial observed requests: fees summary 3.28 seconds, current TVL
0.65 seconds, weekly all-chain TVL history 31.02 seconds, price-neutral history
11.41 seconds. Repeated requests were about 32–289 ms. The browser and parity
checks overlapped later benchmark requests, so these are diagnostic observations,
not an isolated load benchmark; do not classify every first-pass request as cold.

The initial history latency is a deployment blocker to investigate independently
of warm caches. Vercel packaging, CDN behavior, function limits and deployment
region remain unvalidated. A hosted preview deployment still requires approval.

Browser verification completed across four stats tabs: 87 API responses, no HTTP
failures or JavaScript errors. Re-running `publish-hosted --prepare-analytics`
returned the same release ID. Regression suite: 555 passed, 22 skipped.

## History latency investigation

A fresh-process profile did not reproduce the earlier 31-second ordinary-history
request: before this follow-up it took 1.93 seconds. The profile identified 1.29
seconds importing fee-analysis/pricing dependencies unnecessarily, plus a date
lookup scanning the prepared vault-row table (about 1.15 million rows).

TVL requests now avoid the fee-analysis import. Publication stores the complete
ordered date list in `api_tvl_dates`; the reader fetches one small record instead
of deriving dates from vault observations. Re-run publication and grant SELECT on
`api_tvl_dates` when upgrading an existing hosted database. Both local preview
readers have been upgraded with the same release IDs. Regression coverage verifies
that date lookup works even without the vault-row table.

The final fresh-process ordinary-history HTTP request took 0.33 seconds against
Neon. This is an empty application cache, not a forcibly cold Neon database. The
original 31-second event remains unexplained; database cache/compute state and
concurrent load were not captured at the time.

Price-neutral history still reads roughly 166,000 sampled vault rows and scans
reference candidates to preserve timeframe-specific price selection. Profiling
showed database reads/transfer and per-vault calculation dominating that route.
An experiment increasing cursor batches from 2,000 to 10,000 reduced round trips
but did not reliably improve total duration, so it was reverted. Preparing
price-neutral results for supported windows remains further work; no change to
price selection or accounting was made in this investigation.

Final unprofiled HTTP timings after restarting the preview: ordinary history
0.332 seconds; price-neutral history 3.882 seconds. Eight data-parity comparisons
passed. These timings do not prove the original database-cold latency is resolved.

## Personal Vercel deployment preparation

The project `yearn-data-api-preview` was created and linked exclusively in
`rossgalloways-projects` (project `prj_4tcb0tnevKHwxVdOrHsDh3AL2lHz`). No Yearn
team project was modified. The local Vercel build passes with Python 3.12 and the
explicit `handler` class in `api/index.py`. The generated `uv.lock` retains the
resolved dependency versions. API-only routing blocks static source URLs; bundle
checks found no local credentials, database files, data or workspace caches in
the function map. Mapped function contents total approximately 87 MB.

Deployment was authorized and completed on the personal project. The dedicated
read-only Neon URL and logical database name are sensitive **preview-only**
environment variables. No Yearn team project or existing Powerglove configuration
was changed. Vercel preview authentication remains enabled.

Preview endpoint:
https://yearn-data-api-preview-aqh9st2lq-rossgalloways-projects.vercel.app/api/publication

Deployment: `dpl_HSqHj4zq85faqptj3MT83dJe9hH6`, region `iad1`, Python 3.12.
Use `vercel deploy --target=preview --scope rossgalloways-projects` explicitly;
Vercel treats the first deployment of a new project as production by default.
Vercel's environment helper repeatedly prompted despite noninteractive flags;
the sensitive preview variables were configured through the authenticated REST
API with the personal project and team IDs asserted. No secrets are committed.

Authenticated smoke tests use `vercel curl --deployment <preview-url> --scope
rossgalloways-projects`; ordinary browser access requires the owner's Vercel login.
This protected deployment is not yet a public Powerglove production endpoint.

Hosted smoke-test receipt: `artifacts/vercel-preview/results.json` (local, ignored).
All 13 consumer routes returned HTTP 200 JSON, including the Ethereum vault-history
response compressed to approximately 3.01 MB. Source-file URLs return 404. Preview
SSO authentication remains enabled, and the authenticated cache check returned
`x-vercel-cache: MISS`; CDN effectiveness has not been established for public traffic.
First observed hosted fees/price-neutral requests took approximately 12–15 seconds;
this working preview does not establish production latency readiness. The two
credential-less setup deployments, including the automatically promoted initial
production deployment, were deleted. The credentialed preview remains available.


## Git integration

The personal Vercel project is connected to `rossgalloway/yearn-data`; its
production branch is `main`. This API branch sets `git.deploymentEnabled=false`
in `vercel.json`, so pushing it does not deploy. Manual preview deployments remain
available with explicit `--target=preview --scope rossgalloways-projects`.
This branch-level file does not change configuration on other Git branches.
Do not enable production deployment until its environment and release are ready;
the database credentials currently exist only in the Preview environment.

A later production deployment created after connecting GitHub lacks those
preview-only credentials and returns 503. Powerglove continues to use the pinned,
verified preview URL above, not the production alias.
