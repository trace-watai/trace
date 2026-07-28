# TRA-66 live TRACE interface compatibility matrix

Date: 2026-07-28
Source branch inspected: `origin/main`
Source commit: `46a4f7c804da6d817db8d7d6cbae23294435b255`
Linear anchor: TRA-66
Owner: Justin Lam

## Scope

This matrix refreshes TRA-66 against current `origin/main` and the active GitHub PR stack. It supersedes the older June/early-July PR assumptions in the Linear ticket body.

Required column shape from the July 20 TRA-66 comment:

`Artifact or interface | Authoritative producer | Schema/version | Current main state | Every current consumer | Generated fixture | Executable contract test | Open PR / issue | Blocker / consumer gap | DRI | Reviewer | Merge condition | Final main commit / evidence source | Risk level | Next action`

## Current open PR stack

| PR | Linear | Owner / branch | Contract area | Current integration read |
| --- | --- | --- | --- | --- |
| #104 | TRA-73 | Samrath / `trace-harness/tra-73-sample-trace-fixture` | Static dashboard fixture bundle | Stale relative to `main`: branch still shows failure-card/repair-package backend schema `0.2.0`, while `main` has backend examples and schemas at `0.3.0`. Regenerate only after final artifact contracts land. |
| #107 | TRA-43 | Rupert / `runner/tra-batch-suite-execution` | Batch suite execution, run-suite config/result | Mergeable, but must define batch summary artifact and how run config/result/index are enriched before consumers rely on it. |
| #112 | TRA-77 | Emily / `evaluation-core/tra-77-taskspec-requires-escalation` | `TaskSpec.requires_escalation` | Schema bump to `TaskSpec 0.3.0`; must align with TRA-79 verifier behavior and TRA-44 task variants. |
| #117 | TRA-79 | Evan Yang / `trace-harness/tra-79-integrate-escalate-case` | `escalate_case`, missing-info verifier, evidence | Introduces verifier/failure-card version pressure (`VerifierResult 0.3.0`, `FailureCard 0.4.0` on branch). Needs coordination with dashboard and failure-card UI before merge. |
| #119 | TRA-72/UI | Skye / `faliure-card-ui` | Failure-card rendering | Moves dashboard failure-card type to `0.3.0`, matching current backend `main`; conflicts conceptually with #117 if #117 bumps backend to `0.4.0`. |
| #120 | TRA-19 | Samir / `evaluation-core/tra-19-regression-artifact-contract` | Executable/pinned regression artifacts | Moves backend regression artifact to `0.2.0`; dashboard still has `REGRESSION_SCHEMA_VERSION = 0.1.0` on branch. Needs dashboard mirror or explicit non-dashboard merge condition. |

Recently merged PRs that changed the baseline:

| PR | Merged | Contract impact |
| --- | --- | --- |
| #95 | 2026-07-24 | Updated dashboard evidence/failure-card/verifier type contracts, but `origin/main` now still has `apps/dashboard/src/types/failure-card.ts` at `0.1.0` while backend failure-card is `0.3.0`. #119 is the active UI fix. |
| #106 | 2026-07-24 | Added `escalate_case` tool to the environment registry. Current `main` tests are not fully aligned with this addition. |
| #109 | 2026-07-27 | Added refund task variant families under `fixtures/tasks/refund_task_families/`; several directories are still placeholders. Feeds TRA-44/TRA-77/TRA-79. |

## Compatibility matrix

| Artifact or interface | Authoritative producer | Schema/version | Current main state | Current consumers | Generated fixture | Executable contract test | Open PR / issue | Blocker / consumer gap | DRI | Reviewer | Merge condition | Evidence source | Risk | Next action |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `TaskSpec` | `src/trace_harness/tasks/schemas.py` | `TASK_SCHEMA_VERSION = 0.2.0` | Parses task fixtures; includes `available_tools`, `available_docs`, `verifier_ids`, `targeted_failure_modes`, `metadata`; no `requires_escalation` on `main`. | loader, environment, runner, verifiers, regression, dashboard static fixtures. | `fixtures/tasks/*.json`; refund task family variants from #109 now on `main`. | `tests/test_task_schema.py`, `tests/test_task_validation.py`; `validate_task_file`. | #112 / TRA-77 | #112 bumps schema to `0.3.0` and adds `requires_escalation`; consumers must decide if it is authoring metadata only or verifier/runtime contract. | Emily / Evan He | Karan + Rupert + Samrath | Merge #112 only with explicit downstream statement: verifier consumes or ignores `requires_escalation`; fixtures migrated; schema bump documented. | `src/trace_harness/tasks/schemas.py`; `docs/task_validity.md`; PR #112 diff. | High | Katharine audit: inspect #112 against `fixtures/tasks/refund_policy_missing_info.json`, `tests/test_task_validation.py`, and TRA-79 expectations. |
| Task validity rubric | `src/trace_harness/tasks/validation.py`, `docs/task_validity.md` | No separate wire schema | Structural + authoring-quality validation exists; current task-family placeholders mean coverage is uneven. | fixture authors, CI, verifier owners, regression selection. | Same task fixtures. | `tests/test_task_validation.py`. | #112 / TRA-77, #109 merged, TRA-44 | Need validation rules for escalation-required tasks and adversarial variants, not just parseability. | Emily / Evan He | Justin + Karan | No task family should be claimed runnable until it has script coverage and expected verifier behavior. | `fixtures/tasks/refund_task_families/`; TRA-44; #109. | Medium | Add matrix note to TRA-44: placeholders are not runnable coverage. |
| Support environment state | `src/trace_harness/environment/state.py`, `support_env.py` | Pydantic state models, no global schema version | Deterministic in-memory support environment; snapshots become `initial_state.json` and `final_state.json`. | runner, verifier, failure bundles, regression, dashboard fixture. | run artifacts generated by fixture runs. | `tests/test_state.py`, `tests/test_support_env.py`, fixture e2e tests. | #117 / TRA-79, #120 / TRA-19 | Current `origin/main` test run fails in `tests/test_support_env.py`: tests still expect four default tools and missing `env` fixture for hook tests after `escalate_case`/hooks landed. | Evan Yang | Rupert + Karan | Fix environment test contract before claiming environment readiness; update default-tool assertions to include or intentionally exclude `escalate_case`. | `PYTHONPATH=src uv run pytest -q` on `origin/main` snapshot failed: 1 failed, 5 errors. | High | Katharine audit: reconcile `SupportEnvironment` default registry, hook tests, and `escalate_case` side-effect semantics. |
| Tool registry and side effects | `src/trace_harness/environment/tools.py` | Tool args/result models; `ToolSideEffect` enum | `search_docs`, `get_order`, `issue_refund`, `create_ticket`, and `escalate_case` exist on `main`. | runner, trace events, verifier, failure bundles, live-agent readiness. | fixture scripts in `fixtures/scripts/*.json`. | `tests/test_tools.py`, `tests/test_support_env.py`. | #117 / TRA-79 | `escalate_case` is available but canonical missing-info task/verifier integration is still open. Side-effect semantics must be visible to verifier/evidence. | Evan Yang | Karan + Rupert | #117 must show a canonical run where escalation is called, traced, verified, and bundled. | #106 merged; `src/trace_harness/environment/tools.py`; #117 files. | High | Treat `escalate_case` as integration blocker for live readiness until verifier path lands. |
| Retrieval / RAG controls | `src/trace_harness/environment/retrieval.py`, task docs | No separate schema; `retrieval_result` event payload in trace schema `0.2.0` | Simple deterministic document retrieval from task-provided docs. | runner, verifier evidence, attribution, dashboard timeline, live-agent readiness. | `fixtures/docs/refund_docs.json`. | `tests/test_retrieval.py`. | Prior TRA-24 merged; TRA-67 depends on it. | Need evidence that live/non-fixture runs preserve stable retrieval provenance and ranking; otherwise Gemini run is smoke test only. | Evan Yang | Samrath + Karan | Live-readiness checklist must require pinned docs/ranking or explicit experimental-only label. | `src/trace_harness/environment/retrieval.py`; `docs/trace_schema.md`. | Medium | Carry into TRA-67 as required criterion. |
| `AgentAction` / model adapter protocol | `src/trace_harness/models/base.py` | Action schema in Python models; no artifact schema version | `AgentAction` normalizes tool call vs final answer; fixture adapter is default. | runner, transcript builder, trace events, tool environment. | fixture scripts. | `tests/test_fixture_run.py`, CLI/demo tests. | Gemini prior PR not open; TRA-67 still depends on behavior | `src/trace_harness/models/gemini.py` on `main` is still scaffold/config-checked; live adapter readiness is policy work, not done. | Rupert | Samrath + Justin | Do not call Gemini demo-ready until it emits normalized actions, trace `model_response`, and safe provider metadata. | `src/trace_harness/models/base.py`; `src/trace_harness/models/gemini.py`; README says Gemini adapter scaffold. | Medium | TRA-67 should define Experimental / Integration-ready / Demo-ready gates. |
| Runner step loop | `src/trace_harness/runner/agent_runner.py` | Emits run artifacts; uses `RunConfig 0.1.0`, `RunResult 0.1.0` | Deterministic fixture loop and event emission on `main`; timeout wrapper exists. | trace recorder, artifact store, verifier pipeline, dashboard, RunReader. | `runs/<run_id>/` artifacts. | `tests/test_fixture_run.py`, `tests/test_cli.py`. | #107 / TRA-43 | Batch-suite PR introduces pipeline/suite abstractions; must preserve per-run artifact layout and expose batch summary contract. | Rupert | Samrath + Skye | #107 merge condition: batch output has stable location/schema and each child run remains readable via `RunReader`. | `src/trace_harness/runner/agent_runner.py`; #107 files. | High | Add explicit `batch_summary.json` or equivalent row before merging #107. |
| Run config | `src/trace_harness/runner/config.py` | `RUN_CONFIG_SCHEMA_VERSION = 0.1.0`; `PROMPT_VERSION = v0` | Serialized as run artifact; includes provider/model/tool mode/timeout/max steps/metadata. | runner, RunReader, dashboard fixture, batch suite, live readiness. | `run_config.json` from fixture/demo outputs. | `tests/test_fixture_run.py`, `tests/test_run_reader.py`. | #107 / TRA-43 | Batch suite must define config inheritance/effective config; July 20 comment flagged effective config propagation/cost decision. | Rupert | Samrath | Merge only if effective per-run config is materialized, not just implied by suite config. | `src/trace_harness/runner/config.py`; #107. | Medium | In #107 review, check generated child run configs and run index metadata. |
| Run result | `src/trace_harness/runner/result.py` | `RUN_RESULT_SCHEMA_VERSION = 0.1.0` | Status enum: `completed`, `terminated`, `error`; termination reasons include final answer/max steps/script exhausted/error. | CLI, verifier stage, RunReader, dashboard TS mirror, batch suite. | `run_result.json`. | `tests/test_fixture_run.py`, `tests/test_run_reader.py`; dashboard `run-result.ts`. | #107 / TRA-43 | Batch suite must handle terminated/error accounting consistently; July 20 comment called this out. | Rupert | Samrath + Skye | Merge only with test proving batch summary counts completed/terminated/error from child run results. | `src/trace_harness/runner/result.py`; `apps/dashboard/src/types/run-result.ts`; #107. | Medium | Add reviewer question to Rupert/Samrath: where is batch result schema? |
| Trace events | `src/trace_harness/tracing/events.py`, `payloads.py` | `TRACE_SCHEMA_VERSION = 0.2.0` | Backend and dashboard TS mirror both at `0.2.0`; events include `model_response`, `retrieval_result`, `tool_call_*`, `run_finished`, `error`. | verifier, attribution, failure bundles, dashboard timeline, live readiness. | `trace.jsonl`. | `tests/test_tracing_payloads.py`; `apps/dashboard/src/types/trace-event.test.ts`. | #117, Gemini readiness / TRA-67 | `model_response` is reserved for real provider output; live runs must redact raw provider payload and preserve normalized action. | Samrath | Rupert + Skye | Any trace event shape change requires schema bump and dashboard test in same PR. | `docs/trace_schema.md`; `apps/dashboard/src/types/trace-event.ts`. | Medium | TRA-67 must define exactly which provider metadata is allowed in `model_response`. |
| Artifact layout | `src/trace_harness/tracing/artifact_store.py` | Filename constants, no schema version | Single source of truth for run-dir filenames. | RunReader, CLI, verifier, dashboard fixture, API future wrapper. | all files under `runs/<run_id>/`. | `tests/test_artifact_store.py`, `tests/test_run_reader.py`. | #104, #107, #120 | Static fixture (#104), batch (#107), and regression replay (#120) all depend on stable filenames. | Samrath | Skye + Rupert + Samir | Do not merge fixture/batch/replay changes that invent alternate names outside ArtifactStore. | `src/trace_harness/tracing/artifact_store.py`; `docs/future_api.md`. | Medium | In PR review, check every artifact path against ArtifactStore constants. |
| Run index | `src/trace_harness/tracing/run_index.py` | `RUN_INDEX_SCHEMA_VERSION = 0.2.0` | Records run id, task id, status, termination reason, timestamps, provider/model, verifier summary, artifact availability. | RunReader list/summary, dashboard/API future, batch summary. | `runs/index.json`. | `tests/test_run_index.py`, `tests/test_run_reader.py`. | #107 / TRA-43 | Batch runs need index enrichment; July 20 note flagged run-index enrichment. | Samrath | Rupert + Skye | #107 must prove suite runs appear correctly in index and individual run summaries remain stable. | `src/trace_harness/tracing/run_index.py`; #107. | Medium | Add #107 merge condition: test `RunReader.list_runs()` after suite execution. |
| RunReader / read APIs | `src/trace_harness/run_reader.py` | `RunSummary` Python API over artifact schemas | Exists on `main`; future API should be thin wrapper over it. | dashboard/backend, CLI, future API, readiness review. | reads existing run dirs. | `tests/test_run_reader.py`. | #104, #107, #120 | Every generated fixture or batch/replay output must be readable without special-case code. | Samrath | Skye | Merge condition for #104/#107/#120: `RunReader` can read generated artifacts or documented non-run artifacts. | `src/trace_harness/run_reader.py`; `docs/future_api.md`. | Medium | Add dashboard-facing validation path for each artifact row. |
| Verifier input | `src/trace_harness/verifiers/base.py` | `VERIFIER_INPUT_SCHEMA_VERSION = 0.1.0` | Deterministic verifier API over task, trace, final state, run id. | verifier implementations, tests, future CI. | derived from run artifacts. | `tests/test_refund_verifier.py`, verifier fixtures. | #117 / TRA-79 | Missing-info escalation must be expressed as verifier input evidence, not special hidden state. | Karan | Evan Yang + Justin | #117 merge only when verifier input can distinguish correct escalation vs missing/incorrect action. | `src/trace_harness/verifiers/base.py`; #117. | High | Katharine audit: trace from canonical missing-info run to failed/passed checks. |
| Verifier result / failed checks | `src/trace_harness/verifiers/base.py`; dashboard mirror | `VERIFIER_RESULT_SCHEMA_VERSION = 0.2.0` on `main` | Backend and dashboard mirror currently both `0.2.0`. EvidenceKind enum exists. | failure bundles, attribution, regression, dashboard, CI. | `verifier_result.json`; `fixtures/expected/*.json`. | `tests/test_refund_verifier.py`, `tests/test_verifier_fixtures.py`; dashboard `verifier-result.ts`. | #117 / TRA-79 | #117 branch shows `VERIFIER_RESULT_SCHEMA_VERSION = 0.3.0` in dashboard type; backend/version coordination must be checked before merge. | Karan | Darrel + Skye + Samrath | If #117 bumps verifier result, backend + dashboard + expected fixtures + failure bundle generator must land atomically. | `src/trace_harness/verifiers/base.py`; `apps/dashboard/src/types/verifier-result.ts`; #117 branch. | High | Ask Karan/Skye whether #117 is intended schema bump or local branch drift. |
| Evidence item vocabulary | `src/trace_harness/verifiers/base.py`; `apps/dashboard/src/types/evidence.ts` | `EvidenceKind` enum | Current enum-backed evidence kind vocabulary on `main`; consumed by dashboard and failure bundles. | verifier, failure card, dashboard evidence drill-down, attribution. | verifier fixtures. | `tests/test_refund_verifier.py`; dashboard type tests. | #117 | New escalation evidence must use existing enum or bump in backend + TS together. | Karan | Skye + Samrath | No stringly-typed new evidence kind without enum + consumer update. | `src/trace_harness/verifiers/base.py`; `apps/dashboard/src/types/evidence.ts`. | Medium | Add #117 review point: evidence kind for escalation. |
| Attribution result | `src/trace_harness/attribution/schemas.py` | `ATTRIBUTION_SCHEMA_VERSION = 0.3.0` | Heuristic attributor emits root cause and causal explanation for failed verifier result. | failure-card generator, docs, dashboard future. | `attribution_result.json`. | `tests/test_attribution.py`, `tests/test_attribution_validation.py`. | No direct open PR in current stack | Needs to consume new verifier/evidence semantics after #117 without silently misclassifying escalation failures. | Darrel | Karan + Justin | If verifier checks change, attribution mapping tests must be reviewed. | `src/trace_harness/attribution/schemas.py`; `docs/attribution_methodology.md`. | Medium | Add Darrel review to #117 if check IDs/messages change. |
| Failure card | `src/trace_harness/failure_bundles/schemas.py`; dashboard mirror | Backend `FAILURE_CARD_SCHEMA_VERSION = 0.3.0`; dashboard `apps/dashboard/src/types/failure-card.ts` still `0.1.0` on `main` | Backend examples are `0.3.0`; dashboard mirror on `main` is stale. | dashboard failure UI, humans, Linear/PR summaries, repair generation context. | `failure_card.json`; `docs/examples/failure_card.example.json`. | `tests/test_failure_bundle.py`; dashboard PR #119 tests. | #119, #117, #104 | #119 updates dashboard to `0.3.0`, but #117 branch bumps backend failure-card to `0.4.0`. #104 static fixture is stale at `0.2.0`. | Samir + Skye | Samrath + Darrel | Merge #119 before any dashboard fixture relying on failure cards; if #117 requires `0.4.0`, either merge #119 then rebase/bump again or hold #117. | `src/trace_harness/failure_bundles/schemas.py`; `apps/dashboard/src/types/failure-card.ts`; #119; #117; #104. | Critical | Immediate owner sync: Samir/Skye/Evan Yang decide whether escalation count belongs in `0.4.0` before #119/#117/#104 merge. |
| Repair package | `src/trace_harness/failure_bundles/schemas.py`; dashboard mirror | Backend + dashboard `0.3.0` on `main` | Backend and dashboard mirror appear aligned; docs/failure_bundles still says currently `0.2.0` in field table. | dashboard, CI planning, humans. | `repair_package.json`; `docs/examples/repair_package.example.json`. | `tests/test_failure_bundle.py`; dashboard parser. | #104, #117 | Docs table stale; #104 fixture branch generated from `0.2.0`. | Samir | Skye + Darrel | Regenerate docs/fixture after final failure-card/repair package versions settle. | `src/trace_harness/failure_bundles/schemas.py`; `docs/failure_bundles.md`; #104. | Medium | Patch docs/failure_bundles in a follow-up or include in #120/#119 cleanup. |
| Regression artifact | `src/trace_harness/regression/schemas.py`; dashboard mirror | Backend + dashboard `REGRESSION_SCHEMA_VERSION = 0.1.0` on `main` | Non-executable pinned artifact exists; README claims replay exists but #120 implements actual replay/pinned controls. | CI future, dashboard, repair package safety net. | `regression_artifact.json`. | `tests/test_failure_bundle.py`; dashboard parser. | #120 / TRA-19 | #120 bumps backend to `0.2.0` and adds replay/guardrails, but dashboard TS mirror remains `0.1.0` on branch. | Samir | Karan + Skye | Merge #120 only with dashboard mirror update or explicit note that dashboard must not parse `0.2.0` until follow-up. | `src/trace_harness/regression/schemas.py`; `apps/dashboard/src/types/regression-artifact.ts`; #120. | High | Ask Samir/Skye for atomic backend+dashboard regression contract decision. |
| Static dashboard fixture | `apps/dashboard/src/fixtures/refund-failure/*` in #104 | Fixture package, no existing main path | Not present on `main`; PR #104 proposes full fixture bundle. | dashboard offline rendering, contract tests, demo. | PR #104 bundle. | `apps/dashboard/src/fixtures/refund-failure/fixture-contract.test.ts` in #104. | #104 / TRA-73 | Branch fixture uses old failure-card/repair package versions and must be regenerated after #119/#117/#120 decisions. | Samrath | Skye + Samir + Karan | Merge last among artifact-schema PRs; generated fixture must pass dashboard contract tests and match backend schemas on final main. | #104 changed files and branch version inspection. | Critical | Hold #104 until failure-card, verifier-result, regression versions settle. |
| Dashboard run page / TS mirrors | `apps/dashboard/src/types/*`; UI components | Trace `0.2.0`, verifier `0.2.0`, failure card `0.1.0`, repair `0.3.0`, regression `0.1.0` on `main` | Type mirrors exist, but failure-card mirror is stale relative to backend `0.3.0`; actual page currently minimal until #119. | dashboard UI, static fixture, future API. | sample/static data in PRs. | `apps/dashboard/src/types/*.test.ts`; #119 UI tests. | #119, #104, #117, #120 | Dashboard cannot safely consume current backend failure-card without #119; may become stale again if #117/#120 introduce bumps. | Skye | Samrath + Samir + Darrel | Each backend schema bump must include TS mirror and parser tests or explicit non-consumption note. | `apps/dashboard/src/types/`; #119/#104/#117/#120. | Critical | Make dashboard type version table part of TRA-66 owner sync. |
| Batch suite summary | #107 proposed `src/trace_harness/runner/batch.py`, `suite.py`, `pipeline.py` | Not on `main`; schema unknown | No batch summary artifact on `main`. | CLI, run index, dashboard readiness, Sarp delivery view. | `fixtures/suites/*.json` in #107. | `tests/test_suite.py` in #107. | #107 / TRA-43 | Need named artifact/schema for suite result, status counts, child run ids, effective config, and failure aggregation. | Rupert | Samrath + Sarp | Merge only with concrete batch output contract and read path story. | #107 changed files; July 20 TRA-64/66 comments. | High | Ask Rupert for one sample suite output and Samrath for read/index expectations. |
| Gemini / live-provider readiness | `src/trace_harness/models/gemini.py`; TRA-67 | Scaffold on `main`; no CI live calls | Config-checked scaffold; offline fixture remains default. | runner, trace `model_response`, live-readiness/demo narrative. | none in CI. | offline adapter tests only when implemented; no network in CI. | TRA-67 | Live call alone is not TRACE-ready. Needs environment, retrieval, trace, verifier, artifact, dashboard criteria. | Rupert + Justin | Samrath + Evan Yang + Karan + Skye | Keep first demo fixture-first, live-model optional, until integration-ready artifact chain exists. | README, docs/modules.md, TRA-67. | Medium | Use this matrix as input to TRA-67 readiness checklist. |

## Top integration risks

1. Failure-card contract drift is the highest immediate risk.
   - `origin/main` backend failure-card schema is `0.3.0`.
   - `origin/main` dashboard failure-card type is still `0.1.0`.
   - #119 updates dashboard to `0.3.0`.
   - #117 appears to bump failure-card backend to `0.4.0` for escalation count.
   - #104 fixture branch is stale at `0.2.0`.

2. Static dashboard fixture must be regenerated last.
   #104 should not merge until the verifier/failure-card/repair/regression/dashboard contracts settle. Otherwise the offline rendering fixture becomes false evidence.

3. `escalate_case` is partially landed but not integrated end-to-end.
   #106 merged the tool. #117 still owns canonical missing-info run + verifier integration. Current `origin/main` tests fail around support-env defaults/hooks, which is a real integration smell.

4. Regression artifact execution changes are not dashboard-aligned.
   #120 bumps backend regression artifact to `0.2.0`; dashboard mirror remains `0.1.0` unless updated or explicitly excluded from dashboard consumption.

5. Batch-suite output contract is underspecified.
   #107 adds suite/pipeline code, but TRA-66 needs a consumer-facing contract: suite config, effective per-run config, child run ids, status counts, artifact layout, index/read path.

6. Task escalation semantics cross three owners.
   #112 adds `requires_escalation`; #117 verifies escalation; #109/TRA-44 task variants pressure-test it. Emily, Evan Yang, and Karan must align on whether this is a task-authoring requirement, verifier oracle input, or both.

## Recommended review / merge order

1. Fix current `main` support-env test contract.
   - Evidence: `PYTHONPATH=src uv run pytest -q` on `origin/main` snapshot failed with 1 failure and 5 errors in `tests/test_support_env.py`.
   - Owner: Evan Yang / Rupert depending on whether the default tool set or test fixtures are wrong.

2. Resolve failure-card schema target before merging UI/fixture/escalation PRs.
   - Decide whether the next target is `0.3.0` only (#119) or `0.4.0` (#117 escalation count).
   - Owners: Samir, Skye, Evan Yang, Samrath.

3. Review #112 and #117 together.
   - #112 says tasks can require escalation.
   - #117 says canonical missing-info runs/verifier must enforce escalation.
   - Merge condition: fixtures, verifier checks, evidence kind, and failure bundle fields line up.

4. Review #120 with Skye before merge.
   - If regression artifact becomes executable/pinned `0.2.0`, dashboard type mirror must update or dashboard must explicitly not consume it yet.

5. Review #107 with Samrath before merge.
   - Define suite output/read path/index behavior.
   - Require test that suite child runs are readable through existing artifact conventions.

6. Merge/regenerate #104 last.
   - Static fixture must represent final main after schema and artifact contracts settle.

## Stale or misleading Linear parent updates

| Linear | Current problem | Suggested update |
| --- | --- | --- |
| TRA-66 | Original PR list is stale. | Add comment: current open stack is #104, #107, #112, #117, #119, #120; baseline main is `46a4f7c`; failure-card/dashboard/static fixture are the top blockers. |
| TRA-64 | July 20 comment already redefined role but needs current PR facts. | Add risk-log entry with the six current blockers above and assign owner syncs. |
| TRA-67 | Still framed around Gemini but should depend on this matrix. | Add dependency note: live-agent readiness requires controlled environment, retrieval, verifier evidence, read path, failure bundle, regression, and dashboard artifact compatibility. |
| TRA-73 | Static fixture may look mergeable but branch is schema-stale. | Add blocker: regenerate after #119/#117/#120 decisions. |
| TRA-77 | Requires-escalation task field is not isolated. | Add blocker/coordination: must align with TRA-79 verifier semantics and TRA-44 runnable variants. |
| TRA-79 | Escalation integration touches verifier, evidence, failure-card, dashboard. | Add blocker: verify schema bump plan with Skye/Samir/Samrath before merge. |
| TRA-19 | Regression replay PR may change schema. | Add dashboard mirror/read-path acceptance criterion. |
| TRA-43 | Batch suite PR needs consumer contract. | Add acceptance: suite output schema, child run id list, status counts, effective config propagation, run-index integration. |

## Katharine code-audit follow-ups

1. Support environment / `escalate_case` audit
   - Files: `src/trace_harness/environment/tools.py`, `src/trace_harness/environment/support_env.py`, `tests/test_support_env.py`, #117 changed tests.
   - Question: should `escalate_case` be part of default tools, and what fixture/test update makes that contract explicit?
   - Evidence: `origin/main` test failure around default tool count and missing `env` fixture.

2. Failure-card schema audit
   - Files: `src/trace_harness/failure_bundles/schemas.py`, `apps/dashboard/src/types/failure-card.ts`, #119, #117, #104 fixture files.
   - Question: is the next shared failure-card schema `0.3.0` or `0.4.0`, and are all producers/consumers updated atomically?

3. Regression artifact audit
   - Files: `src/trace_harness/regression/schemas.py`, `src/trace_harness/regression/replay.py` in #120, `apps/dashboard/src/types/regression-artifact.ts`, `docs/regression_contract.md` in #120.
   - Question: can dashboard parse the artifact version emitted by replay/materializer after #120?

4. Batch-suite read-path audit
   - Files: #107 `src/trace_harness/runner/batch.py`, `suite.py`, `pipeline.py`, `src/trace_harness/tracing/run_index.py`, `src/trace_harness/run_reader.py`.
   - Question: where does suite-level output live, and how do consumers find child runs and status counts?

## Mohammed / Sarp readiness summary

TRACE is not blocked by one missing module. The immediate readiness blocker is cross-contract sequencing:

- Failure-card backend/dashboard/static fixture versions do not currently line up.
- Escalation has landed as a tool but not yet as a verified canonical behavior.
- Regression replay is becoming executable but needs a dashboard/read-path contract.
- Batch-suite execution needs a stable summary/read path before it can support delivery reporting.
- Live Gemini readiness should remain fixture-first/live-optional until the artifact chain above is stable.

Recommended demo stance now: fixture-first, live-model optional. Do not make live-model required for demo readiness until TRA-67 confirms that provider output produces verifier, failure-card, regression, and dashboard-consumable artifacts.

## Verification notes

Commands/evidence run locally:

```text
git fetch origin
git pull --ff-only
origin/main -> 46a4f7c804da6d817db8d7d6cbae23294435b255
gh pr list --repo trace-watai/trace --state open --limit 50
PYTHONPATH=src uv run pytest -q   # run on clean archive of origin/main
```

Test result on `origin/main` snapshot:

```text
1 failed, 5 errors
FAILED tests/test_support_env.py::test_tool_specs_returns_all_four_tools_by_default
ERROR tests/test_support_env.py::test_no_hooks_dispatch_runs_normally
ERROR tests/test_support_env.py::test_hook_returning_none_is_transparent
ERROR tests/test_support_env.py::test_hook_returning_result_blocks_dispatch
ERROR tests/test_support_env.py::test_multiple_hooks_first_nonnone_wins
ERROR tests/test_support_env.py::test_hook_does_not_fire_on_invalid_call
```

This file was written from the current working branch after fetching/pulling safely. Existing local uncommitted docs were not modified.
