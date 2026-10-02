# TRACE live interface compatibility matrix

Written: 2026-07-28, against `origin/main` through the PR #123 integration
Contract matrix rechecked: 2026-09-29, against `main` at `c93e50a` (#186)
Linear anchor: TRA-66
Owner: Justin Lam

The contract matrix below is kept current, and `tests/test_docs_versions.py`
fails when one of its versions disagrees with `src/`. The executive result,
the acceptance evidence and the release gates are the 2026-07-28 record. Later
progress is noted beside them without rewriting them.

## Executive result (2026-07-28)

The previously conflicting integration stack is now merged. The task model,
escalation flow, verifier, failure bundle, regression replay, batch runner,
run index, dashboard types, visible failure card, and full offline fixture use
compatible contracts on `main`.

This is no longer a schema-sequencing problem. The remaining work is product
completion:

1. run one final-main Gemini acceptance scenario with a team-owned key;
2. finish the incomplete task families and put them in an acceptance suite;
3. turn the dashboard's single failure view into a complete run browser;
4. run and document one release-candidate acceptance pass.

Until those four items are complete, the honest product stance is:
**integrated fixture-backed vertical slice plus live adapter; final live
acceptance and the complete product surface are still outstanding.**

Since then, #179 retained eight key-backed Gemini runs against `main` at
`5e27410` (item 1), and #149 and #150 gave the dashboard a run list and a
trace timeline (part of item 3).

## Contract matrix

| Interface or artifact | Producer and version | Current consumers | Evidence on `main` | Status / remaining gap |
| --- | --- | --- | --- | --- |
| Task definition | `TaskSpec 0.6.0` | loader, runner, verifier, regression | Escalation is a first-class field; validation rejects escalation tasks without the escalation tool. A conditional escalation declares whether the customer made the claim (`claim_made`, #224), and validation rejects an undeclared one. | Compatible. Several task-family directories are still placeholders rather than runnable coverage. |
| Support state | `State 0.2.0` | environment, verifier, failure bundle, regression | Escalations have structured state, sequence IDs, traceable creation steps, and final-state persistence. | Compatible. |
| Tool surface | Environment registry | fixture, live and outside agents | `search_docs`, `get_order`, `issue_refund`, `create_ticket`, and `escalate_case` share validation, tracing, side-effect labels, and hook behavior. | Compatible. |
| Trace | `TraceEvent 0.5.0` | verifier, attribution, dashboard, regression evidence | Backend and TypeScript event vocabularies align; parent event links and structured payloads are tested. Live runs add an optional `call_record` to `model_response` and `error` payloads (#196), including a `model_timeout` error, mirrored in the TypeScript types; fixture traces are unchanged. A billed answer the adapter rejected is kept as a `model_response` before its `model_error`. | Compatible. A live provider still needs to prove safe `model_response` capture and redaction. |
| Run config/result | `RunConfig 0.4.0`, `RunResult 0.1.0` | CLI, index, reader, batch, dashboard fixture | Each child run retains a normal run directory and effective configuration, including optional cassette settings, the live `call_policy` it ran under (null for fixture, replay and outside-agent runs), and the `agent_ref` of an outside agent (#210). | Compatible; `0.1.0` through `0.3.0` configs remain readable, and every retained one is loaded in a test. |
| Run index/read path | `RunIndex 0.6.0`, `RunReader` | CLI, batch reporting, bundle stage, dashboard, public results uploader, future API | Verification enriches the canonical index; list-runs shows PASS/FAIL; terminated runs stay distinct. Each bundled run carries the `bundle_key` the bundle stage looks cards up by (#211), and a rebuild recovers it from cards and pointers. | Compatible; an older index is rebuilt at `0.6.0` on first read. The dashboard reads the index through `run-loader.ts` and lists every retained run. |
| Verifier result | `VerifierResult 0.4.0` | failure bundle, regression, dashboard | Backend and TypeScript align, including escalation-record evidence, missing-escalation checks, and the three-state `verdict` (pass, fail, incomplete). | Compatible. |
| Attribution | `AttributionResult 0.4.0` | failure-card generator, audit consumers | Attribution from `HeuristicAttributor` is validated against trace/evidence and preserves ambiguity notes. It records the first step a control blocked as `block_step` and what the agent did next as `post_block_outcome` (#157). | Compatible for the current refund vertical; `0.3.0` and older results load with both fields null. |
| Failure card | `FailureCard 0.5.0` | visible dashboard card, offline fixture | Backend and TypeScript align on structured blast radius, including escalation count. Since #211 there is one card per root cause. Each card carries a `bundle_key` and lists its `occurrences`, and a reproduction's directory holds `bundle_ref.json` naming the run with the card. The dashboard follows the pointer and lists the occurrences. | Compatible and visibly rendered, with multi-run navigation from #149 and the trace timeline from #150; `0.4.0` cards load with no key and no occurrences. |
| Repair package | `RepairPackage 0.3.0` | dashboard-ready contract, humans, regression planning | Controls are linked to actual verifier checks and have priority, location, behavior, impact, and tradeoffs. | Compatible. Not yet rendered as a complete dashboard section. |
| Regression artifact | `RegressionArtifact 0.3.0` | replay CLI, dashboard contract | Pinned state, docs, normalized agent actions, verifier checks, positive siblings, and control replay are tested. | Compatible and executable. |
| Suite config/summary | `Suite 0.5.0`, `BatchSummary 0.5.0` | CLI, delivery reporting, `branch` | Child run IDs, verdict counts, termination counts, cost coverage, and canonical artifacts are emitted and tested. A suite may set `max_cost_usd`; the summary's `budget` block records spend, `budget_exhausted` or `budget_unenforceable`, and the cells never run. A `branch` batch also carries its experiment and condition in `metadata`, and each entry its `condition`, `seed`, divergence and `post_block_outcome` (#159). An agent config may name an outside agent with `provider: external` and `agent_ref` (#210), and declare `billing: subscription` for one whose calls run on a plan (Suite 0.5.0), whose entries then record the agent's reported `notional_cost_usd` (BatchSummary 0.5.0). | Compatible; older suites and summaries remain readable and run uncapped. Unknown live-provider cost remains `null` and is never counted as zero by the budget guard. Fixture and replay runs cost exactly zero, and so does a live run that got no answer when every failed attempt carried an HTTP error status. An outside agent's cost is always `null`, so under a cap its config is refused as `budget_unenforceable`, unless it declares subscription billing, which is admitted without a charge and never priced as zero. |
| Experiment plan/result | `Experiment 0.4.0` (`ExperimentSpec`, `ExperimentResult`) | `experiment record`, `experiment freeze`, `list-experiments`, `branch`, `validate-control` | The plan is written before anything runs and freezes what must not change; the result maps each condition to its batch and records the eight metrics from the #27 memo and a decision. 0.2.0 added the frozen set (#195), 0.3.0 the continuation script (#159), 0.4.0 the keep rule (#203). | Compatible; a 0.1.0 plan predates the frozen set and records without one, and a plan without a keep rule is written as older code reads it. |
| Live sweep | `SweepSpec 0.1.0`, `SweepSummary 0.1.0` | `run-sweep`, `retain-sweep` | A sweep runs every suite task under one or more live models for one or more seeds, records every call to a cassette, writes each provider's cells as an ordinary batch, and caps spend in USD (#198). | Compatible. |
| Bundle pointer | `BundleRef 0.1.0` | bundle stage, `RunReader.get_bundle`, index rebuild | A reproduction's run directory holds `bundle_ref.json` naming the key and the `canonical_run_id` whose directory holds the card (#211). | Compatible. |
| Control library | `ControlLibrary 0.2.0` | `controls`, `--control-library`, `replay --apply-control --commit` | Accepted controls with provenance, status history and SHA-256-pinned evidence (ADR-0003). 0.2.0 entries record their acceptance basis (#228). | Compatible; a 0.1.0 entry reads as advisory with its replay mode not recorded. |
| Repair validation | `RepairValidation 0.3.0` | `replay --apply-control --commit`, control library promotion, metrics history | Each control verdict records the artifact's replay mode and whether it gates, and re-runs record their task fixture; the rollup reports over-blocking by task family with a 95% upper bound (#228). | Compatible; a verdict without a basis record reads as unsupported and advisory. |
| Metrics history | `MetricsSnapshot 0.3.0` | metrics history job, dashboard `/metrics` | One snapshot per commit in `docs/acceptance/metrics_history.jsonl`. 0.2.0 splits accepted controls into gating and advisory, 0.3.0 adds task-family over-blocking counts and a 95% upper bound (#228). | Compatible. The dashboard's TypeScript mirror moved to `0.3.0` with #228. |
| Attribution score | `AttributionScore 0.1.0` | `score-attribution` | Scores a registered attribution method against a JSONL label file with the C1 formulas, per field with no average, and writes `attribution_score.json` (#189). Only the heuristic is registered. | Compatible. |
| Public results SQL | `RESULTS_SCHEMA_VERSION = 0.1.0` | uploader, Supabase `RunReader` backend | Runs, batches and experiments with their artifacts as jsonb, defined in `supabase/migrations/`, with a lockstep test against the newest migration (#205). | Compatible. An artifact schema bump changes only jsonb content and needs no migration. |
| Full offline run fixture | Deterministic 11-artifact bundle | dashboard tests and offline demo | Generated from the current pipeline; all run IDs and artifact links align; regeneration is byte-for-byte deterministic. | Compatible and current. |
| Dashboard contracts/UI | TypeScript mirrors listed above | browser UI | Format, lint, typecheck, tests, and production build pass. A run list, a run detail page and a trace timeline render from retained runs, and the timeline marks attribution steps and shows each step's failed checks. | Partial product surface. The dedicated verifier failures (#169) and attribution (#170) views, the repair package (#171) and regression artifact (#172) panels, and any backend connection remain. The dashboard reads the runs directory off disk. |
| Gemini/live adapter | `src/trace_harness/models/gemini.py` | CLI and suite runner | Native function calling is implemented with current `google-genai`; the shut-down Gemini 2.0 default was replaced with `gemini-3.6-flash`; suite temperature, seed, and timeout reach both the persisted config and adapter; provider responses precede normalized actions in the trace; parallel calls fail explicitly. Calls go through the shared policy (#196): status read from the error's `code`, httpx connection, timeout and proxy errors retried while `LocalProtocolError` and `UnsupportedProtocol` fail on the first attempt, Gemini's `retryDelay` honored, paced to 10 requests/minute by default. Usage comes from `usage_metadata`, with thinking tokens billed as output, and is priced from `GEMINI_PRICING`. The request config is a dict the SDK validates into the same model, so the full call path is tested against a fake client. | Contract-compatible, offline-tested, and proved live. #179 retained eight key-backed runs under `docs/acceptance/live-gemini-2026-09-13/`, recorded before the shared call policy and pricing from #196. The `gemini-3.6-flash` price in `GEMINI_PRICING` doubles on 2027-01-01 and has to be updated that day. |
| Anthropic/live adapter | `src/trace_harness/models/anthropic.py` | CLI and suite runner | Native tool use through the optional `anthropic` SDK (1.x). Tool results are paired by `tool_use` id, carried across turns in `provider_state` the same way Gemini's thought signature is, and a turn with no id of its own gets a stable one. The turn's `thinking` and `redacted_thinking` blocks ride there too and go back unmodified in front of the `tool_use`, as Sonnet 5 and later require. Token usage, cache reads and writes included, is read off the response and priced from a module price table checked against Anthropic's pricing page on 2026-09-24, so `cost_usd` on a live batch entry, direct or recorded through a cassette, is a number rather than null. `tool_choice` sets `disable_parallel_tool_use`, and a response with two calls still fails explicitly. A truncated turn (`max_tokens`, context window) is a model error with its billed response kept in the trace. Calls go through the shared policy (#196): 408, 409, 429, 529 overloaded and every other 5xx except 501 are retried with backoff and `Retry-After`; other 4xx errors and 501 fail on the first attempt; the SDK's own retries are off; and calls are paced to 50 requests/minute by default. | Contract-compatible and offline-tested against a fake SDK module with the real package absent. One key-backed acceptance run is still outstanding, held on a team-owned key. A seed is recorded in `run_config.json`, marked `seed_sent: false`, and never sent, because the Messages API has no seed, so runs seeded against the two providers are not comparable on reproducibility. A temperature for a model that rejects one (Sonnet 5, Opus 4.7 and later, Fable) is refused at construction. |
| OpenAI/live adapter | `src/trace_harness/models/openai.py` | CLI and suite runner | Native function calling through the optional `openai` SDK (3.x). Tool arguments arrive as a JSON string and are parsed at the boundary, where a string that will not parse is an adapter error. Tool messages pair by `tool_call_id`, carried in `provider_state`. `parallel_tool_calls` is disabled on the request rather than dropping extra calls after the fact. Cached prompt tokens are priced at the cached rate, and a turn cut off at the length limit is a model error with its billed response kept in the trace. Calls go through the shared policy (#196): 408, 409, 429 and every 5xx except 501 are retried with backoff and `Retry-After`; a 429 with `insufficient_quota`, other 4xx errors and 501 fail on the first attempt; the SDK's own retries are off; and calls are paced to 500 requests/minute by default. | Contract-compatible and offline-tested against a fake SDK module with the real package absent. One key-backed acceptance run is still outstanding. The seed is sent, as the Gemini adapter's is, and `system_fingerprint` is recorded, since a seeded re-run whose backend build moved is not a reproduction. A temperature for a reasoning model (the gpt-5 family, the o-series) is refused at construction, since at their default reasoning effort they reject it. |

## Integrated decisions now in force

- A task that requires escalation must expose `escalate_case`; otherwise task
  validation fails.
- The canonical refund failure is intentionally a four-part failure: it misses
  required escalation, issues an unauthorized cash refund, writes an
  unsupported outage claim, and relies on deprecated policy.
- Backend schema changes and dashboard mirrors land together. Static fixtures
  are regenerated after contract changes and are guarded by executable tests.
- Suite reporting separates completed, terminated, and errored runs.
- Cost reporting distinguishes a known zero-dollar fixture run from missing
  provider telemetry.
- A regression is not only a document: it pins the world and agent actions,
  replays the failure, applies a control, and protects positive sibling cases.

## Acceptance evidence (2026-07-28)

Verification on the merged stack as it stood on 2026-07-28. The counts are that
day's and have grown since. `scripts/check_repo.sh` and the dashboard gate give
the current ones.

```text
Repository gate:
- Ruff check: passed
- Ruff format check: passed
- Python tests: 379 passed
- end-to-end pipeline smoke: passed

Dashboard gate:
- formatting: passed
- lint: passed
- TypeScript typecheck: passed
- tests: 30 passed
- production build: passed

Fixture generation:
- all 11 required run artifacts produced
- all schema/version and run-linkage contract tests passed
- two consecutive generations produced identical SHA-256 hashes
```

## Release gates and owners (2026-07-28)

Since then, #179 retained eight key-backed Gemini runs against `main` at
`5e27410` (see the Gemini row above). Apart from the final acceptance owner,
which now names the TPM lane, the rows are as written on 2026-07-28.

| Gate | Concrete completion condition | Primary owner | Required reviewers |
| --- | --- | --- | --- |
| Live provider | With a team-owned key, one controlled final-main run produces a normal trace, verdict, artifacts, index entry, and readable summary; the retained provider-response fields and retry/cost limitations are documented. | Rupert | Samrath, Karan, Justin |
| Complete task bank | Fill approval, customer wording, policy-order/status, refund type, and retrieval-completeness families with runnable task + script + explicit expected checks + positive sibling; include them in an acceptance suite. | Emily with Evan He | Karan, Rupert |
| Complete dashboard | Load the canonical run read path; show a run list and selection, outcome, trace timeline, evidence, failure card, repair package, regression artifact, and clear empty/loading/error states. | Skye | Samrath, Samir |
| Final acceptance | Run the complete suite on the release candidate, record exact PASS/FAIL/terminated/error counts, inspect every blocking failure, and publish one go/no-go note. | TPM | Justin, Karan, all feature owners |

## Change discipline

Any future contract change is merge-ready only when the same change includes:

1. producer version bump where required;
2. every current consumer update;
3. generated fixture refresh;
4. executable backend and dashboard contract tests;
5. evidence that the normal run/index/read path still works.

This file should be refreshed from live `main`, not from old ticket descriptions
or branch-local assumptions.
