# Module guide — where to work and what the rules are

One section per module under `src/trace_harness/`. Each answers: what
belongs there, what it exposes, the rules that keep it healthy, and what to
build next. Owners live in [team_ownership.md](team_ownership.md);
architecture-level data flow lives in [architecture.md](architecture.md).

---

## tasks/ — task schemas and loading *(Emily Au, Evan He)*

**What belongs here:** the `TaskSpec` schema (what a TRACE test scenario
*is*), the shared `Severity` enum, and the loaders that turn JSON fixtures
into validated objects (`load_task`, `load_docs_for_task`).

**Exposes:** `TaskSpec` (consumed by environment, runner, verifiers,
regression), `load_task(path)`, `load_docs_for_task(task, task_path)` — the
only sanctioned way fixture JSON enters the system.

**Rules:** tasks describe, they never act — no tool logic, no verifier
logic, no scenario-specific Python (scenario content is fixture *data*).
`extra="forbid"` stays: fixture typos must fail at load time.

**Build next:** promote `metadata.user_message` to a first-class field;
a `validate-fixtures` CLI command for CI; parameterized task variants that
sweep boundary values (day 29/30/31/60/61) from one template.

## models/ — model adapters *(Rupert Maiti)*

**What belongs here:** the provider-neutral contract types (`Message`,
`ToolSpec`, `ToolCall`, `AgentAction`), the `ModelAdapter` protocol, and
its implementations: `FixtureModelAdapter` (deterministic scripted agent —
the default everywhere) and `GeminiModelAdapter` (scaffold; the provider
call is an explicit `NotImplementedError` with the intended design in its
docstring).

**Exposes:** `ModelAdapter.next_action(transcript, tools) -> AgentAction`;
`create_model_adapter(provider, ...)` — the only place provider strings are
interpreted.

**Rules:** no code path may make tests need an API key. No tool execution
(environment), no prompt construction (runner), no retry frameworks until
the first real adapter forces a deliberate design.

**Build next:** implement `GeminiModelAdapter.next_action` (recorded-
response tests only, never live calls); decide the parallel-tool-call story
(`AgentAction` grows a list form behind a schema bump); a `replay` adapter
that re-feeds a recorded trace's actions.

## environment/ — the sandboxed world *(Evan Yang)*

**What belongs here:** typed state (`state.py`), tool definitions with
declared side-effect classes (`tools.py`), the tool registry
(`registry.py`), deterministic keyword retrieval (`retrieval.py`), and the
environment facade the runner drives (`support_env.py`).

**Exposes:** `SupportEnvironment` (satisfies the runner's `ToolEnvironment`
protocol: `tool_specs`, `validate_call`, `execute`, `side_effect_for`,
`snapshot_state`); `SupportState` (snapshots become
`initial_state.json`/`final_state.json`); `ToolDefinition`/`ToolRegistry`;
`search_docs()`.

**Rules:** every tool declares a side-effect class (`read_only` /
`external_durable` / `external_irreversible`) — attribution depends on it.
Retrieval never truncates content and always carries doc `status`.
Everything stays deterministic: no clocks, no randomness, no network.
`issue_refund` intentionally permits unsafe refunds today so the verifier
has something real to catch; the future guardrail seam is marked in
`support_env.execute` — install controls there, not inside handlers. No
pass/fail judgment here (verifiers), no prompt text (runner), no vector DB
until keyword retrieval demonstrably fails a real task.

**Build next:** pre/post execution hooks so guardrails compose (first
consumer: the refund guardrail from the repair package); doc chunking and
pluggable scorers behind the same `search_docs` signature; a second
workflow environment to force the generic/support split.

## runner/ — the execution engine *(Rupert Maiti)*

**What belongs here:** `AgentRunner` (the step loop), `RunConfig` (every
knob affecting a run, persisted for reproducibility), `RunResult`, and the
`ToolEnvironment` protocol (defined here, at the consumer, so environments
never import the runner).

**Exposes:** `AgentRunner(adapter, environment, artifact_store).run(task,
config) -> RunResult`; `build_initial_transcript` (prompt version `v0` —
bump `RunConfig.prompt_version` when it changes).

**Rules (these are the architecture):** the runner never imports tool
implementations or a global tool registry; it contains zero scenario
knowledge (if a change mentions refunds, it belongs elsewhere); it never
runs verifiers (separate pipeline stage over artifacts); every run —
including crashes — leaves a run directory with `run_result.json`.
`status=completed` means "produced a final answer", never "was correct".

**Build next:** a timeout that can interrupt a hung provider call (today
checked only between steps); deliberate retry/backoff design for real
adapters; `model_response` events when the first real adapter lands;
multi-run orchestration (N runs, varied seeds) once live models make runs
non-deterministic.

## tracing/ — events, recording, artifacts *(Samrath)*

**What belongs here:** the `TraceEvent` schema (`events.py`), the
write-through JSONL recorder (`recorder.py`), and `ArtifactStore`
(`artifact_store.py`) — the single source of truth for the
`runs/{run_id}/` layout.

**Exposes:** `TraceEvent` + `TraceEventType` (the contract every consumer
reads — see [trace_schema.md](trace_schema.md)); `TraceRecorder.record()`
(flushed per event so dying runs keep partial traces); `ArtifactStore` and
its filename constants — **the only sanctioned spelling of artifact
names**.

**Rules:** this folder is the data contract for the future API and
dashboard; renaming a field or file breaks them — coordinate and bump
`schema_version`. No event *interpretation* here (that's
verifiers/attribution), no database before local JSON actually hurts.

**Build next:** typed per-event-type payload models once the dashboard's
first read pass shows what it consumes; `parent_event_id` for nested
spans; atomic writes (tmp + rename); a run index file.

## verifiers/ — deterministic pass/fail *(Karan Gupta)*

**What belongs here:** code that decides whether a finished run was
*actually correct*, returning structured `VerifierResult`s with evidence —
never bare booleans.

**Exposes:** `Verifier.verify(task, trace, final_state, run_id) ->
VerifierResult` (deterministic, side-effect free, robust to partial
traces); `VerifierResult`/`FailedCheck`/`EvidenceItem`; the
`verifier_id -> class` registry (`registry.py`); `RefundPolicyVerifier`,
whose policy rules load from the current policy doc's `metadata.rules` —
data, not hardcode.

**Rules:** the verifier decides pass/fail; no LLM verdict may block or
unblock a release ([verifier_philosophy.md](verifier_philosophy.md)).
Check ids are public contract (repair packages and regression artifacts
link to them) — renaming one is a breaking change. **Overblocking is a
verifier bug:** every blocking check ships with positive tests proving
legitimate behavior passes (boundary days, manager approval, store-credit
paths, deprecated-doc *mention* vs *reliance*). Checks that cannot run
become warnings, not crashes or silent passes.

**Build next:** structured citations in traces to replace substring
provenance matching; claim-matching beyond regex for ticket grounding
(with a labeled set of tricky texts); a second domain verifier to
pressure-test the base contract; real multi-verifier semantics (today: a
simple merge).

## attribution/ — failure explanation *(Darrel Wihandi)*

**What belongs here:** schemas (`schemas.py`) and the rule-based MVP
attributor (`heuristic.py`) that explain *where and why* a verified
failure happened.

**Exposes:** `HeuristicAttributor.attribute(task, trace, verifier_result)
-> AttributionResult` (requires a *failed* verifier result; raises on
passed ones); `FailureCategory` (extend, never repurpose values).

**Rules:** `root_cause_step`, `missed_recovery_step`,
`first_unrecoverable_step`, and `first_irreversible_action_step` are
different concepts — never collapse them (refund fixture: root cause 3,
first irreversible 5; tested). Heuristic confidence is capped at 0.85.
When the trace exposes no reasoning, attribution must say evidence was
limited — and still produce what it can. A judge never overrides a
verifier's verdict ([attribution_methodology.md](attribution_methodology.md)).
`FailureCategory` definitions, neighbor boundaries, and primary/contributing
selection rules live in [failure_taxonomy.md](failure_taxonomy.md).

**Build next:** a judge schema emitting the same `AttributionResult` so
heuristic and LLM judges are comparable; a human-labeled agreement set
(with Justin/Katharine) before trusting either; a per-workflow strategy
interface for the disconfirming-evidence detector (the current one is
refund-domain-specific).

## failure_bundles/ — cards and repair packages *(Samir Mohammed)*

**What belongs here:** turning a verified failure into the human-readable
`FailureCard` and the engineering `RepairPackage`
([failure_bundles.md](failure_bundles.md)); the generator orchestrates
regression materialization too.

**Exposes:** `FailureBundleGenerator.generate(...) -> FailureBundle`;
`FailureCard`, `RepairPackage`, `RepairControl`.

**Rules:** MVP output is template-assembled from deterministic signals —
verifier checks select controls (check id → builder in
`_CONTROL_BUILDERS`), attribution supplies the narrative, blast radius is
*computed* from final state. Nothing pretends to be LLM analysis. Every
control names a real installation seam, a deterministic check, behavior on
failure, and its tradeoff — a control that can't name its seam is a wish.
No artifacts for passing runs (the generator raises).

**Build next:** markdown rendering of failure cards for PR/ticket bodies;
control templates for new check ids as new verifiers land; de-duplication
across bundles (same root cause in N runs → one card, not N).

## regression/ — rerunnable regression artifacts *(Samir Mohammed, with Karan)*

**What belongs here:** materializing verified failures into pinned,
rerunnable `RegressionArtifact`s.

**Exposes:** `materialize_regression_artifact(...)`;
`RegressionArtifact`/`SiblingTest`.

**Rules:** pin the run's *recorded* state and docs (snapshots, not live
fixtures — fixtures may evolve). Failed check ids are the assertion set.
`positive_sibling_tests` (from task metadata) are mandatory in spirit — a
fix that breaks the sibling is overblocking. This loop is TRACE's
differentiation over pure attribution (see
[AGENTRX_TRACE_SUMMARY.md](AGENTRX_TRACE_SUMMARY.md)).

**Build next:** `trace-harness replay <regression_artifact.json>` that
runs directly from pinned state; a CI collector that executes every
blocking regression plus its positive siblings; a promotion flow (run
artifact → reviewed → committed fixture).

---

## tests/ — conventions

Tests live in top-level `tests/`, run with bare `pytest`, and are offline
forever — a test that needs an API key or network is a regression by
definition. They consume the *real* fixtures under `fixtures/` (no
synthetic copies that could drift) and write into pytest temp dirs, never
the repo's `runs/`. Unit tests for verifiers use neutral synthetic
customers — don't extend scenario fixtures to make a unit test pass.
Whoever owns a module owns its tests; new checks, event types, and
artifacts all need tests in the matching `test_*.py`. When a pinned
expectation (`fixtures/expected/`) breaks deliberately, update it in the
same PR and say so.
