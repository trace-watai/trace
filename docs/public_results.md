# Public results

Owner is Evaluation Systems. TPM creates the Supabase project and holds its
keys. Tracks #205 (TRA-127). This replaces `future_api.md`, and its last
section keeps the local API plan that file used to hold.

The evidence retained under `docs/acceptance/` is hosted in a Supabase project
so it can be browsed without cloning the repository. A static site cannot read
JSON files from a runs directory, so the hosted copy is what a public site
reads. Anyone with the project URL and the anonymous key can read every
retained run, batch and experiment. Nobody with that key can write.

## What is hosted

The hosted set is exactly what `RunReader` serves for the retained tree. Each
table row holds the JSON a `RunReader` method returns, which is the JSON the
pipeline writes, `schema_version` included. Nothing is reshaped.

| Table | Key | Column | `RunReader` method |
|---|---|---|---|
| `runs` | `run_id` | `summary` | `list_runs()`, and `list_runs_for_batch(id)` through `batch_id` |
| | | `run_result` | `get_run(id)` |
| | | `task_spec` | `get_task(id)` |
| | | `trace` | `get_trace(id)`, a JSON array of trace events |
| | | `verifier_result` | `get_verifier(id)`, null until verified |
| | | `attribution_result` | `get_attribution(id)`, null until attributed |
| | | `failure_card`, `repair_package`, `regression_artifact` | `get_bundle(id)`, all three or none |
| | | `canonical_run_id` | `get_bundle(id)` for a reproduction (#211), the run whose row holds its card |
| `batches` | `batch_id` | `summary` | `get_batch_summary(id)` |
| | | `suite_report` | `get_suite_report(id)` |
| `experiments` | `experiment_id` | `spec`, `result` | `list_experiments()`, `get_experiment(id)`, `result` null until recorded |
| `schema_versions` | `version` | `description`, `applied_at` | none, it records the applied SQL schema |

Every artifact column is `jsonb`. The only typed columns are the natural keys,
`runs.task_id`, and `runs.batch_id`, the one filter `RunReader` needs. Check
constraints tie them to the JSON they came from, and a row whose JSON lacks
the key is refused too. The checks compare with `is not distinct from`,
because `->>` yields null for a missing key and a check that evaluates to null
passes. The natural keys use the `C` collation, so `order=run_id.asc` lists
runs in the code point order `RunReader` uses, whatever the database's default
collation. Each row also carries `content_sha256`, which the uploader uses to
skip unchanged rows.

Today that is 15 runs (five from the `refund_v0` batch of 20 August, eight live
Gemini runs of 13 September, and the two reference-agent runs of 23
September), two batches (that `refund_v0` batch and a `refund_bundles_v0` batch
of 17 September) and one experiment.

## What is not hosted

- `initial_state.json`, `final_state.json` and `run_config.json`. `RunReader`
  has no method for them. The provider and model a run used are in its summary.
- Cassettes, `repair_validation.json`, regression gate summaries,
  `metrics_history.jsonl`, sweep summaries, and markdown such as `report.md`,
  `suite_report.md` and the retained folders' READMEs.
- Anything that is not retained under `docs/acceptance/`, including everything
  in `runs/`.
- Writes of any kind from a client, and accounts or sign-in. Both are out of
  scope for #205.

Hosting one of these starts with a `RunReader` method for it. The table follows
in a new migration.

## Reading it

With the project URL and the anonymous key, every table is readable over
Supabase's REST API (PostgREST). The key goes in the `apikey` header. A legacy
JWT anon key also goes in `Authorization: Bearer`.

```sh
curl "$TRACE_SUPABASE_URL/rest/v1/runs?select=summary&order=run_id.asc" \
  -H "apikey: $TRACE_SUPABASE_ANON_KEY"
curl "$TRACE_SUPABASE_URL/rest/v1/runs?select=trace&run_id=eq.run_20260913T150039Z_0f2f19b7" \
  -H "apikey: $TRACE_SUPABASE_ANON_KEY"
```

A response holds at most 1000 rows, Supabase's default. Ask with
`Prefer: count=exact` and page with `limit` and `offset` until the total in
`Content-Range`.

From Python, `TRACE_RUN_READER=supabase` swaps the backend behind
`list-runs` and `list-experiments`, and `trace_harness.run_readers.open_run_reader`
gives the same choice to any caller. A project that cannot be reached or
refuses the key makes those commands print the error and exit 2.

```sh
TRACE_RUN_READER=supabase \
TRACE_SUPABASE_URL=https://<ref>.supabase.co \
TRACE_SUPABASE_ANON_KEY=<publishable or anon key> \
  trace-harness list-runs
```

`SupabaseRunReader` (`src/trace_harness/run_reader_supabase.py`) has every
read method of `RunReader` with the same signature, return types and missing
states. An unknown run raises `RunNotFound`, an artifact not produced yet is
`None`, and an unknown batch or experiment raises `FileNotFoundError`. It
refuses a key that maps to `service_role`. The filesystem `RunReader` is
unchanged and stays the default. Unset, empty and `filesystem` all select it,
and any other value is an error.

For the dashboard, each `jsonb` column is the file of the same name, so the
parsers in `apps/dashboard/src/types/` apply to it unchanged. Wiring a hosted
data source into the dashboard is frontend work and is not part of #205.

## How rows get there

`python -m trace_harness.public_results.upload docs/acceptance` does five
things in order.

1. Stage. `public_results/retained.py` copies every retained run directory,
   batch summary and experiment into one runs directory in a temp dir. A run
   directory is one holding `run_result.json`, a batch summary is a file named
   `batch_summary.json` or ending in `_batch_summary.json`, and an experiment is
   a directory holding `experiment.json`. Only the files directly in an
   experiment folder are the experiment's own, and staging goes on into its
   subfolders, so runs retained inside it, such as the fork points of a branch
   experiment, are staged as runs. Two sources with the same id are an error.
   Index files are never copied or read.
2. Read. The staged copy is read with the filesystem `RunReader`, which
   rebuilds the index from the artifacts. The upload therefore never depends
   on the index or its format, and reading never rewrites the retained
   `index.json` in place. That file predates index schema 0.5.0, so
   `RunReader` would otherwise rebuild it on the spot.
3. Build rows. `public_results/rows.py` turns each `RunReader` answer into a
   row with `model_dump(mode="json")`, the serialization `ArtifactStore` writes
   with, and hashes it.
4. Plan. The uploader reads each table's keys and `content_sha256`, and sorts
   rows into new, changed and unchanged.
5. Write. New and changed rows are upserted on the natural key with
   `Prefer: resolution=merge-duplicates`, in requests under 1 MB. With
   `--prune`, hosted rows that are no longer retained are deleted.

A run that reproduced an earlier failure card holds `bundle_ref.json` in place
of its own card, repair package and regression artifact (#211). Staging reads
the pointer, and the run's row keeps the three bundle columns null and names
the run holding the card in `canonical_run_id`. `SupabaseRunReader.get_bundle`
follows it the way the filesystem reader follows the pointer. The card is
hosted once, so a card that gains an occurrence re-uploads one row. Staging
refuses a pointer to a run that is not retained or holds no card, and names
both runs, because the hosted row would otherwise show a bundled failure as
unbundled. A run that holds a card of its own is its own bundle home, whatever
pointer sits beside it.

It is idempotent. A rerun over the same tree finds every hash equal and sends
no write at all. Even a forced rewrite of every row would replace each row with
an identical one on the same key. The tests check both on an in-memory
stand-in, and on a real Postgres, where no row's `xmin` moves on the second
run.

The hash covers every column and the SQL schema version, so a new schema
version re-uploads everything once. It leaves out one field. A suite report
that was never persisted is built in memory on every read and stamped with the
current time, so its `generated_at` is not hashed. The hosted value is the time
of the upload that last changed that batch.

Other guards stop a bad upload before anything is written. `--prune` refuses
to take any table down to zero rows, so a staging mistake that loses every run,
batch or experiment cannot wipe that table, and `--allow-empty-prune` is the
explicit way to empty one. The uploader refuses to write unless
`schema_versions` records the version its code expects. It refuses a key it
can tell is the anonymous one, and a project URL whose port is not a number.
`--dry-run` plans against the project without writing, and `--offline` builds
the rows and reports their size with no network.

## The publish job

`publish-results` in `.github/workflows/integration-ci.yml` runs on a push to
`main`, after both the backend and the dashboard gates.

- It skips itself when a secret is missing. Whether `TRACE_SUPABASE_URL` and
  `TRACE_SUPABASE_SERVICE_KEY` are both set is lifted into a job-level
  `PUBLISH` flag, and every step checks the flag, because a step-level `if`
  cannot read secrets. Without both secrets the job logs why it skipped and
  stays green, the same pattern `metrics-history` uses for its token. It never
  runs on a pull request.
- The two secrets are set in the env of the upload step alone. Checkout,
  `pip install` and the key scan run without the service key in their
  environment, and the job-level env holds only the `true` or `false` of
  `PUBLISH`. A structural test pins both.
- Uploads run one at a time (`concurrency: publish-results`). A run also steps
  aside when `main` has moved on to a different retained tree, so gates that
  finish out of order cannot let an older commit overwrite or prune what a
  newer one retained. A newer commit that only appends metrics history does not
  count as a change.
- Before the upload, `python -m trace_harness.public_results.secret_scan`
  greps `docs/acceptance/` and every `cassettes` folder for key patterns and
  fails on a hit. It covers Google `AIza` and `AQ.` keys, Anthropic, OpenAI,
  Supabase secret keys and access tokens, JWTs such as the legacy
  `service_role` key, GitHub and AWS credentials, private keys, and
  authorization or api key headers. A trace or cassette keeps a model's text
  as a JSON string, sometimes JSON inside JSON, and a URL keeps it
  percent-encoded, so each line is matched as written and again with its JSON
  and `%XX` escapes decoded. A key right after a `\n` or a `%20` is caught.
  Hits are printed redacted to four characters and a length, because the log
  of a public repository is public.
  The same scan runs as a test in the backend gate, so a committed key fails
  the pull request before it reaches `main`.
- It fails loudly on `main` when the secrets are present and something is
  wrong, for example a migration that was never applied.

## Row level security

Every table has row level security enabled and one policy, `select` for `anon`
and `authenticated`. There is no insert, update or delete policy. The
migration also revokes the privileges Supabase grants on new tables by default
and grants back only `select` to those two roles. A write with the anonymous
key is therefore refused twice. Without a privilege it fails with
`permission denied` (PostgREST answers 401, code 42501). If a privilege were
ever granted by mistake, row level security still refuses an insert and makes
an update or delete match no rows.

`service_role`, which the secret key maps to, bypasses row level security by
design. It keeps `select`, `insert`, `update` and `delete` on the result tables
and loses `truncate`. That key exists only as a repository secret.

`tests/test_public_results_sql.py` applies the migrations to a throwaway
Postgres cluster and acts as each role with `set role`, which is how PostgREST
switches roles. It checks that anon and authenticated read every row, that
every write they try is refused, that row level security alone refuses them
once write privileges are granted back, and that no table in `public` lacks
row level security. The tests skip when no PostgreSQL server binaries are
installed. GitHub's Ubuntu runners include them, and they ran locally on
PostgreSQL 16.14.

## Schema versions and upstream bumps

The SQL layout is a versioned contract like every JSON artifact. Migrations
live in `supabase/migrations/` and are never edited once applied. A change is
a new migration that inserts its version into `schema_versions` and bumps
`RESULTS_SCHEMA_VERSION` in `src/trace_harness/public_results/schema.py`. A
lockstep test checks the constant and every column name against the
migrations.

| Version | Migration | Change |
|---|---|---|
| 0.1.0 | `20260923120000_public_results_0_1_0.sql` | `runs`, `batches`, `experiments` and `schema_versions` (#205) |

A change upstream reaches the tables in one of three ways.

- An artifact schema bump, such as `FailureCard` or `RunIndexEntry` in #211,
  `Experiment` in #203 or a batch summary change, needs no migration. The
  `jsonb` columns take the new JSON, the content hash changes, and the next
  push to `main` re-uploads the affected rows. The hosted reader validates
  with the harness's current models, which already have to read the older
  retained files on disk.
- Renaming or removing a field behind a typed column (`run_id`, `task_id`,
  `batch_id`, `experiment_id`) fails the check constraints at upload. That
  failure is deliberate, and the fix is a migration.
- A new kind of result, such as `SweepSummary` from #198, is hosted once
  `RunReader` can read it. That takes a new migration with a table or column,
  a version bump and the matching `SupabaseRunReader` method. The signature
  test fails the gate as soon as `RunReader` gains a method the hosted reader
  lacks.

## Free tier sizing

Measured on 24 September 2026 with `scripts/measure_public_results.py`, which
prints every figure below. The sweep is two providers by five seeds by the 32
tasks of `refund_v0`, or 320 runs, all hosted, although #198 plans to retain
only the failing cells.

| Quantity | Measured |
|---|---|
| `docs/acceptance/`, 139 files | 1,144,146 bytes |
| One retained run directory on disk, min / mean / max over 15 | 43,990 / 71,470 / 104,462 bytes |
| One hosted run row as JSON, min / mean / max | 34,965 / 60,529 / 88,026 bytes |
| Hosted rows as JSON, 15 runs + 2 batches + 1 experiment | 907,929 + 50,625 + 1,701 bytes |
| Postgres size of the retained set, tables with TOAST and indexes | 884,736 bytes |
| A stored run row as a share of its JSON, min / mean / max | 41.5 / 51.9 / 70.2 percent |
| One sweep as JSON, 320 run rows copied from the retained rows largest first, plus two 160-entry batches | 19,497,081 + 372,894 bytes |
| Postgres size of the retained set plus that sweep | 11,968,512 bytes (11.4 MiB) |
| `list_runs()` response for all 335 runs | 149,225 bytes |

The script builds the rows the way the uploader does, loads them into a
throwaway PostgreSQL 16.14 cluster through the same statement PostgREST runs
for an upsert, and reads `pg_total_relation_size` after `vacuum analyze`. A
stored row's share is the sum of `pg_column_size` over its columns, which
counts a TOASTed value at its compressed size, over the row's JSON. TOAST
compression stores a run row in 42 to 70 percent of its JSON size, 52 percent
on average.

Supabase's Free plan allows a 500 MB database and 5 GB of egress a month, with
a limit of two active projects. A Free project goes read-only above 500
MB, and database size counts data, indexes and materialized views across all
databases, excluding WAL. The retained set plus one sweep is about 12 MB, under
3 percent of that. The empty project's own system schemas also count, and
their size was not measured here, because no project exists yet. At about 11
MB per fully hosted sweep, dozens of sweeps fit. For egress, 5 GB is about
80,000 full run reads at the mean row size, or about 33,000 loads of the whole
run list, before any compression.

Free projects are paused after a week without enough database activity. Each
publish reads the project, and site traffic counts too, but a week with no push
to `main` and no visitors can pause it. Restoring it is one click in the
dashboard, and a scheduled read would keep it awake if pausing becomes a
problem.

These limits were checked on 23 September 2026 against
[Supabase pricing](https://supabase.com/pricing), the pages on
[database size](https://supabase.com/docs/guides/platform/database-size) and
[project pausing](https://supabase.com/docs/guides/platform/free-project-pausing),
and the page on [API keys](https://supabase.com/docs/guides/api/api-keys).

## Regenerate locally

Everything the project would hold can be rebuilt from the repository with no
project and no keys.

```sh
# Build every row from docs/acceptance/ through RunReader and write it out.
python -m trace_harness.public_results.upload docs/acceptance --offline --dump /tmp/public-results

# The hosted reader against the filesystem reader, method by method, and the
# schema, row level security and idempotence on a throwaway Postgres.
pytest tests/test_run_reader_supabase.py tests/test_public_results_sql.py \
  tests/test_public_results_upload.py tests/test_public_results_rows.py

# The key scan the publish job runs.
python -m trace_harness.public_results.secret_scan

# The free tier sizing table, on a throwaway Postgres.
PYTHONPATH=src:tests python scripts/measure_public_results.py
```

To try the migration by hand, apply it to any Postgres that has the `anon`,
`authenticated` and `service_role` roles (`tests/pg_cluster.py` creates them).

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --single-transaction \
  -f supabase/migrations/20260923120000_public_results_0_1_0.sql
```

There is no Supabase project behind the tests, so their responses are
synthesized in PostgREST's shape, and they are named that way.
`tests/postgrest_fake.py` holds an in-memory stand-in and one that runs the
same requests as SQL against the throwaway Postgres.
`tests/fixtures/postgrest/synthesized_retained.json` is recorded from the
in-memory stand-in over rows built from the real retained artifacts, and is
replayed like a cassette. It fails loudly if a retained artifact changes, and
`PYTHONPATH=src:tests python tests/postgrest_fake.py --write-fixture`
regenerates it.

## Setting up the project

TPM does this once.

1. Create a project on the Free plan in the team's Supabase organization.
   Keep the Data API enabled. Store the database password in the team's
   password manager.
2. Apply the migration. Paste
   `supabase/migrations/20260923120000_public_results_0_1_0.sql` into the SQL
   Editor and run it, or run the `psql` command above with the connection
   string from the project's Connect panel. Then
   `select * from public.schema_versions;` shows `0.1.0`.
3. From the project's API keys settings, note the project URL, the
   publishable key and a secret key. A project that only shows the legacy
   `anon` and `service_role` keys works the same way with those.
4. In the repository's Actions secrets, add `TRACE_SUPABASE_URL` (the
   `https://<ref>.supabase.co` URL) and `TRACE_SUPABASE_SERVICE_KEY` (the
   secret key). The secret key goes nowhere else.
5. Publish by re-running the latest workflow on `main`, or by the next push to
   `main`. The job log ends with `published to https://<ref>.supabase.co`, and
   a second run ends with `nothing to publish`.
6. Check the anonymous side with the publishable key. `list-runs` as shown
   above lists as many runs as the upload log reported for the `runs` table.
   A write is refused with 401 and code `42501`.

   ```sh
   curl -i -X POST "$TRACE_SUPABASE_URL/rest/v1/experiments" \
     -H "apikey: $TRACE_SUPABASE_ANON_KEY" -H "Content-Type: application/json" \
     -d '{"experiment_id": "probe", "spec": {"experiment_id": "probe"}}'
   ```

7. Give the project URL and the publishable key to the frontend. Both are
   public by design.

Scoping the two secrets to a GitHub environment limited to `main` is optional
hardening.

## Local API

Supabase supersedes the FastAPI wrapper for public reads. The hosted tables
are the shared query surface the old start condition waited for, where someone
needs the same query twice, and they need no server of ours.

The wrapper stays the plan for a local server if one is ever needed, for
example to serve a runs directory that is not retained. It would be a thin
layer over `RunReader` with no read logic of its own, serving the artifact
schemas unchanged, with no auth. The method map it would follow is below, and
`SupabaseRunReader` answers the same methods.

| Endpoint | `RunReader` method | Returns |
|---|---|---|
| `GET /runs` | `list_runs()` | `list[RunSummary]` |
| `GET /runs/{id}` | `get_run(id)` | `RunResult` |
| `GET /runs/{id}/task` | `get_task(id)` | `TaskSpec` |
| `GET /runs/{id}/trace` | `get_trace(id)` | `list[TraceEvent]` |
| `GET /runs/{id}/verifier` | `get_verifier(id)` | `VerifierResult \| None` |
| `GET /runs/{id}/attribution` | `get_attribution(id)` | `AttributionResult \| None` |
| `GET /runs/{id}/bundle` | `get_bundle(id)` | `FailureBundle \| None` |
| `GET /batches/{id}` | `get_batch_summary(id)` | `BatchSummary` |
| `GET /batches/{id}/runs` | `list_runs_for_batch(id)` | `list[RunSummary]` |
| `GET /batches/{id}/report` | `get_suite_report(id)` | `SuiteReport` |
| `GET /experiments` | `list_experiments()` | `list[ExperimentSpec]` |
| `GET /experiments/{id}` | `get_experiment(id)` | `ExperimentSpec` and `ExperimentResult \| None` |

If the wrapper wants a different shape, that starts a schema conversation with
the harness, and the wrapper itself never transforms what `RunReader` returns.
Its start condition is a local consumer that can read neither the runs
directory nor the hosted tables. Until then there is no `apps/api/`.
