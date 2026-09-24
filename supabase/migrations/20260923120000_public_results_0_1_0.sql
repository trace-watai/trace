-- TRACE public results, SQL schema 0.1.0 (#205, TRA-127).
--
-- Hosts what RunReader serves for the runs retained under docs/acceptance/,
-- so the evidence can be browsed without cloning the repository. One row per
-- run, per batch and per experiment. Every artifact is a jsonb column holding
-- the harness's own JSON for it, schema_version included. Typed columns are
-- limited to the natural keys, runs.task_id and the one filter RunReader
-- needs (runs.batch_id), so a bump to an artifact schema upstream changes
-- row contents only and leaves the table layout as it is.
--
-- Check constraints tie each typed column to the JSON it was taken from. They
-- compare with "is not distinct from", because ->> yields null for a key the
-- JSON lacks and a check whose expression is null passes. The keys use the C
-- collation, so ordering by them gives the code point order RunReader lists
-- in, whatever collation the database defaults to.
--
-- Anonymous and signed-in clients may only read. The uploader in CI writes with
-- the service key, whose Postgres role (service_role) bypasses row level
-- security. Writes by anon or authenticated are refused twice, once because
-- those roles hold no write privilege and once because no policy admits a
-- write, so dropping either layer by mistake still leaves the tables read only.
--
-- This file is a released contract. Once applied anywhere it is never edited.
-- A change is a new migration with the next version, recorded in
-- schema_versions below and in RESULTS_SCHEMA_VERSION
-- (src/trace_harness/public_results/schema.py).
--
-- Apply as one transaction, for example
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --single-transaction -f <this file>


-- Which SQL schema versions have been applied. The uploader refuses to write
-- until the version its code expects is present here.
create table public.schema_versions (
    version text primary key,
    description text not null,
    applied_at timestamptz not null default now()
);

comment on table public.schema_versions is
    'TRACE public results: applied SQL schema versions (docs/public_results.md).';


-- One row per retained run. Each jsonb column is what the RunReader method of
-- the same purpose returns: summary is list_runs(), run_result is get_run(),
-- task_spec is get_task(), trace is get_trace() as an array of events,
-- verifier_result, attribution_result and the three bundle artifacts are
-- get_verifier(), get_attribution() and get_bundle(), null until produced.
--
-- A run that reproduced an earlier failure card (#211) holds bundle_ref.json
-- in place of its own bundle. Its row keeps the three bundle columns null and
-- holds that pointer in bundle_ref, which is get_bundle_ref(), and get_bundle()
-- reads the card from the row the pointer's canonical_run_id names. The card is
-- stored once, so a card that gains an occurrence changes one row.
create table public.runs (
    run_id text collate "C" primary key,
    task_id text not null,
    batch_id text,
    summary jsonb not null,
    run_result jsonb not null,
    task_spec jsonb not null,
    trace jsonb not null,
    verifier_result jsonb,
    attribution_result jsonb,
    failure_card jsonb,
    repair_package jsonb,
    regression_artifact jsonb,
    bundle_ref jsonb,
    content_sha256 text not null,
    constraint runs_summary_matches_keys check (
        (summary ->> 'run_id') is not distinct from run_id
        and (summary ->> 'task_id') is not distinct from task_id
        and (summary ->> 'batch_id') is not distinct from batch_id
    ),
    constraint runs_result_matches_key check (
        (run_result ->> 'run_id') is not distinct from run_id
    ),
    constraint runs_trace_is_array check (jsonb_typeof(trace) = 'array'),
    -- The bundle stage writes all three artifacts together, and get_bundle()
    -- returns all three or none.
    constraint runs_bundle_is_whole check (
        (failure_card is null) = (repair_package is null)
        and (failure_card is null) = (regression_artifact is null)
    ),
    constraint runs_bundle_ref_matches_keys check (
        bundle_ref is null or (
            (bundle_ref ->> 'run_id') is not distinct from run_id
            and (bundle_ref ->> 'task_id') is not distinct from task_id
        )
    ),
    -- A reproduction holds no bundle of its own and names another run.
    constraint runs_reproduction_holds_no_bundle check (
        bundle_ref is null
        or (
            failure_card is null
            and (bundle_ref ->> 'canonical_run_id') is not null
            and (bundle_ref ->> 'canonical_run_id') is distinct from run_id
        )
    ),
    constraint runs_content_sha256_is_hex check (content_sha256 ~ '^[0-9a-f]{64}$')
);

create index runs_batch_id_idx on public.runs (batch_id) where batch_id is not null;

comment on table public.runs is
    'TRACE public results: one row per retained run, artifacts as jsonb (docs/public_results.md).';


-- One row per retained batch. summary is get_batch_summary(), suite_report is
-- get_suite_report().
create table public.batches (
    batch_id text collate "C" primary key,
    summary jsonb not null,
    suite_report jsonb not null,
    content_sha256 text not null,
    constraint batches_summary_matches_key check (
        (summary ->> 'batch_id') is not distinct from batch_id
    ),
    constraint batches_report_matches_key check (
        (suite_report ->> 'batch_id') is not distinct from batch_id
    ),
    constraint batches_content_sha256_is_hex check (content_sha256 ~ '^[0-9a-f]{64}$')
);

comment on table public.batches is
    'TRACE public results: one row per retained batch (docs/public_results.md).';


-- One row per retained experiment. spec and result are the pair
-- get_experiment() returns, result null until one has been recorded.
create table public.experiments (
    experiment_id text collate "C" primary key,
    spec jsonb not null,
    result jsonb,
    content_sha256 text not null,
    constraint experiments_spec_matches_key check (
        (spec ->> 'experiment_id') is not distinct from experiment_id
    ),
    constraint experiments_result_matches_key check (
        result is null or (result ->> 'experiment_id') is not distinct from experiment_id
    ),
    constraint experiments_content_sha256_is_hex check (content_sha256 ~ '^[0-9a-f]{64}$')
);

comment on table public.experiments is
    'TRACE public results: one row per retained experiment (docs/public_results.md).';


-- Row level security. Only a select policy exists, so for anon and
-- authenticated an insert fails the policy check and an update or delete
-- matches no rows, even if a write privilege were granted by mistake.
alter table public.schema_versions enable row level security;
alter table public.runs enable row level security;
alter table public.batches enable row level security;
alter table public.experiments enable row level security;

create policy schema_versions_public_read on public.schema_versions
    for select to anon, authenticated using (true);
create policy runs_public_read on public.runs
    for select to anon, authenticated using (true);
create policy batches_public_read on public.batches
    for select to anon, authenticated using (true);
create policy experiments_public_read on public.experiments
    for select to anon, authenticated using (true);


-- Privileges. Supabase grants every privilege on new tables in public to anon,
-- authenticated and service_role by default. Take them all back and grant only
-- what each role needs, so a write by anon is refused before row level
-- security is even consulted, and the key held in CI can upsert and prune but
-- not truncate or alter.
revoke all on table
    public.schema_versions, public.runs, public.batches, public.experiments
    from anon, authenticated, service_role;

grant select on table
    public.schema_versions, public.runs, public.batches, public.experiments
    to anon, authenticated;

grant select on table public.schema_versions to service_role;
grant select, insert, update, delete on table
    public.runs, public.batches, public.experiments
    to service_role;


insert into public.schema_versions (version, description)
values ('0.1.0', 'runs, batches and experiments with artifacts as jsonb (#205)');
