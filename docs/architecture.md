# Architecture

TRACE is an agent reliability harness. It runs agents through realistic
workflows, captures execution traces, verifies outcomes deterministically,
localizes failures, and turns them into durable artifacts.

## The canonical loop

```
task
→ target-agent execution
→ structured trace
→ deterministic verifier
→ attribution / judge
→ failure card
→ repair package
→ regression test
→ dashboard
```

Core principle: **the verifier decides pass/fail; the judge explains why.**
Hard correctness is deterministic wherever possible. LLMs may classify,
summarize, and propose repairs — they are never the sole authority for
release-blocking correctness.

## Module map and data flow

```
TaskSpec (fixtures/tasks/*.json, loaded by tasks/loader.py)
   │
   ▼
AgentRunner (runner/agent_runner.py)
   │  asks ModelAdapter for actions        (models/base.py — fixture | gemini)
   │  drives ToolEnvironment               (environment/support_env.py)
   │     └─ tools + typed state + retrieval (environment/{tools,state,retrieval}.py)
   │  records through TraceRecorder        (tracing/recorder.py)
   ▼
ArtifactStore — runs/{run_id}/             (tracing/artifact_store.py)
   │  task_spec / run_config / initial_state / trace.jsonl / final_state / run_result
   ▼
Verifier (verifiers/refund_policy.py via verifiers/registry.py)
   │  → verifier_result.json  (pass/fail + failed checks + evidence)
   ▼
Attribution (attribution/heuristic.py)
   │  → attribution_result.json  (root cause / missed recovery / irreversible / categories)
   ▼
FailureBundleGenerator (failure_bundles/generator.py + regression/materializer.py)
   │  → failure_card.json / repair_package.json / regression_artifact.json
   ▼
Dashboard & API (future — consume the run directory as-is;
                 contracts pinned in docs/future_api.md / docs/future_dashboard.md)
```

The CLI (`trace_harness/cli.py`) exposes each arrow as a subcommand
(`run-fixture`, `verify`, `attribute`, `bundle`) and chains them as
`run-pipeline`. **Stages communicate only through artifacts on disk** — any
stage can be re-run later (e.g. re-verify an old trace with a new
verifier), and the dashboard sees exactly what the pipeline saw.

## Dependency rules (what keeps this modular)

- `tasks/schemas.py` and `models/base.py` are leaf modules — everything may
  import them; they import nothing of ours.
- The **runner** defines the `ToolEnvironment` protocol it needs;
  environments satisfy it structurally and never import the runner.
- The runner never imports tool implementations or registries — the
  environment owns tools and mutable state.
- Verifiers/attribution/bundles consume artifacts (task, trace, state
  dicts) — they never reach into a live environment.
- Nothing outside `tracing/artifact_store.py` spells artifact filenames.

## Key boundaries and their owners

| Boundary | Contract | Owner |
|---|---|---|
| Task fixtures → loader | `TaskSpec` (extra=forbid) | Emily |
| Adapter ↔ runner | `ModelAdapter.next_action(transcript, tools) → AgentAction` | Rupert |
| Runner ↔ environment | `ToolEnvironment` protocol; `ToolResult` with side-effect class | Rupert + Evan Yang |
| Everything ↔ trace | `TraceEvent` + run-directory layout | Samrath |
| Trace/state → verdict | `VerifierResult` with `FailedCheck`s | Karan |
| Verdict → explanation | `AttributionResult` (step fields are distinct concepts) | Darrel |
| Explanation → artifacts | `FailureCard` / `RepairPackage` / `RegressionArtifact` | Samir |
| Artifacts → UI/API | the `runs/{run_id}/` files themselves | Skye + Samrath |

Every schema carries `schema_version`. Changing a contract = bump the
version + update consumers + tests + the matching doc, in one PR.

## Determinism doctrine

Fixture mode is byte-deterministic apart from run ids and timestamps
(tested in `test_fixture_run.py`). No clocks in state (ages are stored as
day counts), no randomness, no network. This is what makes verifier
development, regression replay, and dashboard fixtures trustworthy. Live
model runs will be non-deterministic — that's fine; the *harness around
them* must not add noise of its own.

## Observability: TRACE-native artifacts are the source of truth

The run directory — `trace.jsonl` plus the JSON artifacts — is the
canonical record of every run. Verifiers, attribution, bundles, the CLI,
the future dashboard, and the future API all consume these files first.

Langfuse, Arize Phoenix, OpenTelemetry, and similar tools are **optional
future export/viewer layers**: adapters that translate TRACE events
outward, never sources of truth and never local dependencies. The harness
must always run, verify, and replay with nothing but Python and the
filesystem. If an exporter lands, it belongs behind the trace schema
(Samrath's seam) as a one-way translation of recorded events — pipeline
stages must never read from an external observability backend.

## Agent-session metadata (Entire.io)

The team uses Entire (from Entire.io) so commits carry coding-agent session
context — TRACE is a tool for understanding agent work, and its own repo
should be legible that way too. Follow the installation instructions from
Entire.io and document the session in your PR. If setup fails, do not block
work — note it in the PR instead. See CONTRIBUTING.md.

## What is deliberately absent (see ADR-0001)

No vector DB, no Docker/Kubernetes, no hosted services, no database, no
live LLM calls in tests, no dashboard implementation. Each becomes worth
adding only after the thing it replaces demonstrably hurts.
