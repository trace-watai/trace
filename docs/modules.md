# Module guide — where to work and what the current shape is

One section per module under `src/trace_harness/`. Each answers: what
belongs there, what it exposes, the current design defaults, and ideas for
what to build next. Owners live in [team_ownership.md](team_ownership.md);
architecture-level data flow lives in [architecture.md](architecture.md).

> **This guide is a strawman, not law.** It describes how the scaffold was
> built so you don't have to reverse-engineer it — it is not a list of
> agreed-upon rules. If you own a section, rewrite it: edit or delete the
> "Rules" and "Build next" parts for your area as your design evolves,
> rather than working around them. The "Build next" lists are suggestions
> to seed your backlog, not commitments.
>
> Only a handful of things are actual repo-wide constraints (agreed in the
> founding spec, enforced by tests/CI):
> 1. Tests run offline — no API keys, no network, ever.
> 2. The deterministic verifier decides release-blocking pass/fail; an LLM
>    never does.
> 3. Schema changes bump `schema_version` and update consumers + tests in
>    the same PR.
> 4. Generated `runs/` output is never committed; scenario content lives in
>    `fixtures/`, not `src/`.
>
> Everything else in this file — naming, heuristics, design boundaries,
> even "never collapse X and Y" style statements — is the current default,
> owned by whoever owns the module, and changeable through a normal PR.

---

## tasks/ — task schemas and loading *(Emily Au, Evan He)*

**What belongs here:** the `TaskSpec` schema (what a TRACE test scenario
*is*), the shared `Severity` enum, and the loaders that turn JSON fixtures
into validated objects (`load_task`, `load_docs_for_task`).

**Exposes:** `TaskSpec` (consumed by environment, runner, verifiers,
regression), `load_task(path)`, `load_docs_for_task(task, task_path)` — the
only sanctioned way fixture JSON enters the system; `validate_task` +
`python -m trace_harness.tasks.validation` for authoring-quality checks.

**Rules:** tasks describe, they never act — no tool logic, no verifier
logic, no scenario-specific Python (scenario content is fixture *data*).
`extra="forbid"` stays: fixture typos must fail at load time. Two validation
layers: structural (schema) vs authoring-quality (`validation.py` + the rubric
in [task_validity.md](task_validity.md)); counterexamples live in
`fixtures/tasks/counterexamples/`.

**Build next:** promote `metadata.user_message` to a first-class field (pending
Rupert on transcript shape); wire `validate-fixtures` into `check_repo.sh`/CI
(with Sarp); parameterized task variants that sweep boundary values
(day 29/30/31/60/61) from one template.

## models/ — model adapters *(Evaluation Systems)*

**What belongs here:** the provider-neutral contract types (`Message`,
`ToolSpec`, `ToolCall`, `AgentAction`), the `ModelAdapter` protocol, and
its implementations. `FixtureModelAdapter` is the deterministic scripted
agent and the default everywhere. `GeminiModelAdapter` and
`AnthropicModelAdapter` and `OpenAIModelAdapter` are the three live
providers, each normalizing native tool calling into the same single-action
contract through its own optional SDK. Three vendors exist so a live result
never depends on one credential, which is what #158, #159 and #217 need to
compare model families at all.

**Exposes:** `ModelAdapter.next_action(transcript, tools) -> AgentAction`;
`create_model_adapter(provider, ...)` — the only place provider strings are
interpreted; `ForkAdapter(prefix, continuation, switch_at_step)` (`fork.py`),
which serves recorded actions through `switch_at_step` and delegates every
later step, so the branch stage forks a run without the runner knowing
([branch_stage.md](branch_stage.md)).

**Provider capability, since they are not interchangeable:**

| Provider | Native tool calling | Seed | Published price | Default pacing |
| --- | --- | --- | --- | --- |
| `gemini` | yes | yes | `gemini-2.5-flash`, `-flash-lite` and the default `gemini-3.6-flash`, whose price doubles on 2027-01-01 | 10 requests/minute |
| `anthropic` | yes | no, the Messages API has none | yes | 50 requests/minute |
| `openai` | yes | yes, best-effort with `system_fingerprint` | yes | 500 requests/minute |

A seeded sample plan, such as the five seeds per condition in #217, can only
run against a provider whose seed is actually sent.

**Rules:** no code path may make tests need an API key. No tool execution
(environment) and no prompt construction (runner). Provider errors become
`ModelAdapterError`; multiple parallel calls must fail explicitly until the
shared action contract supports them.

| Mode | Configuration | Behavior |
| --- | --- | --- |
| Fixture (default) | `--provider fixture` | Runs a scripted fixture; no cassette, SDK, or key. |
| Live | `--provider gemini` | Calls Gemini using explicit model settings. Needs `GEMINI_API_KEY` and the `gemini` extra. Token usage is read from `usage_metadata`, with thinking tokens counted as output, and priced from `GEMINI_PRICING`. A model missing from that table, the default included, reports a null cost. |
| Live | `--provider anthropic` | Calls Claude using explicit model settings. Needs `ANTHROPIC_API_KEY` and the `anthropic` extra. Token usage is read off the response and priced, so `cost_usd` is a number. A seed is recorded and never sent, because the Messages API has none. |
| Live | `--provider openai` | Calls an OpenAI chat model. Needs `OPENAI_API_KEY` and the `openai` extra. Priced the same way. The seed is sent, and the response's `system_fingerprint` is recorded so a seeded re-run whose backend build moved can be told apart from a real reproduction. |
| Record | `--cassette-mode record` | `RecordingModelAdapter` wraps the selected provider and writes normalized responses. |
| Replay | `--cassette-mode replay` | Reads recorded responses without constructing a provider; a missing or mismatched request is an error. |

`--cassette-dir` selects the root (default `fixtures/cassettes`). Files use
`<task_id>/<model_id>/<seed>.jsonl`, with URL-escaped path components and
`default` for an unspecified seed. Recording refuses to overwrite a cassette.
The factory accepts an explicit `CassetteConfig`; environment variables never
select record/replay. Suite agent configs accept the same `cassette` object.

Each versioned entry pins its step, transcript hash (including provider state),
tool-declaration hash, provider, resolved model, temperature, seed, timeout,
and prompt version. `run_config.json` retains those settings, cassette mode,
and resolved path. `RunConfig` and `SuiteSpec` added cassettes in `0.2.0` and
are `0.3.0` since #196; older data remains readable with cassettes disabled.
Changing a setting or request requires a new recording. Replay never falls back
to the network. An entry recorded from a live adapter also keeps the step's
token counts under the provider's own usage key and its `call_record`, so the
recorded run is priced and the replay shows the same retries. A replay itself
calls nothing, so its `cost_usd` is exactly zero.

Run traces retain fresh audit IDs and timestamps. Deterministic comparisons
exclude only those two event fields; all remaining trace bytes, tool outcomes,
and verifier results must agree. See [cassette fixtures](../fixtures/cassettes/README.md)
for an offline Gemini example and provenance.

**Live call policy (#196):** every live adapter sends its SDK call through
`LiveCaller` in `models/policy.py`. It retries transient errors (408, 409, 429,
5xx except 501, and connection failures) with exponential backoff and jitter,
honors a provider's `Retry-After` or Gemini's `retryDelay`, and never retries a
permanent error (other 4xx, OpenAI's `insufficient_quota`) or anything that is
not a provider error, such as `ProviderNotConfiguredError`. Refusals and content
filters are rejected after the call returns and are never retried. Each
provider is paced to a minimum spacing between requests, shared by the whole
process. The runner hands each call its remaining time, and the policy gives up
with outcome `deadline` before a retry or wait would pass it. The SDKs' own
retries are off (`max_retries=0`), so every attempt is recorded: the
`CallRecord` rides on `AgentAction.call_record` into the `model_response`
event, or on the error into the `error` event, and cassettes keep it so a
replay shows the same retries. `run_config.json` records the policy as
`call_policy` (`RunConfig 0.3.0`); a suite agent config may override it.

**Build next:** decide the parallel-tool-call story (`AgentAction` grows a list
form behind a schema bump); keep one controlled key-backed acceptance run
outside CI; update the `gemini-3.6-flash` price line on 2027-01-01.

## environment/ — the sandboxed world *(Evan Yang)*

**What belongs here:** typed state (`state.py`), tool definitions with
declared side-effect classes (`tools.py`), the tool registry
(`registry.py`), deterministic keyword retrieval (`retrieval.py`), reference
guardrails (`guardrails.py`), controls as data (`controls.py`), and the
environment facade the runner drives (`support_env.py`).

**Exposes:** `SupportEnvironment` (satisfies the runner's `ToolEnvironment`
protocol: `tool_specs`, `validate_call`, `execute`, `side_effect_for`,
`snapshot_state`); `SupportState` (snapshots become
`initial_state.json`/`final_state.json`); `ToolDefinition`/`ToolRegistry`;
`search_docs()`; `ControlInstance` + `GUARDRAIL_REGISTRY` + `reference_controls()`
(`controls.py`) and `SupportEnvironment.install_control` /
`uninstall_control` / `installed_controls` — the only way a guardrail gets
installed by id. An unknown `guardrail_ref`, or a `rule_ref` that doesn't
match the rules its guardrail reads, fails at install time. A call blocked
by an installed control comes back with `ToolResult.blocked_by` set to its
`control_id`, which the runner copies into the `tool_call_executed` and
`tool_observation` trace events (trace schema 0.4.0).

Three seams exist for controls. `register_pre_execute_hook` runs before a
handler and can prevent the side effect. `register_post_execute_hook` runs
after one and sees the result, so it can reject a record that should not stand,
though the side effect has already happened. `register_final_answer_hook` runs
on the answer itself, which never reaches the environment otherwise, and a
block there ends the run as terminated with `blocked_by` on the `final_answer`
event (trace schema 0.5.0). `install_control` refuses a control that reads the
same rules through the same guardrail as an installed one but disagrees on
`behavior_on_failure`, because ordering would otherwise decide the outcome and
nobody decided the ordering.

**Rules:** every tool declares a side-effect class (`read_only` /
`external_durable` / `external_irreversible`) — attribution depends on it.
Retrieval never truncates content and always carries doc `status`.
Everything stays deterministic: no clocks, no randomness, no network.
`issue_refund` intentionally permits unsafe refunds today so the verifier
has something real to catch; guardrails run as pre-execute hooks in
`support_env.execute` — install them as controls (`install_control`), not
inside handlers. No
pass/fail judgment here (verifiers), no prompt text (runner), no vector DB
until keyword retrieval demonstrably fails a real task.

**Build next:** post-execute hooks; doc chunking and
pluggable scorers behind the same `search_docs` signature; a second
workflow environment to force the generic/support split.

## runner/ — the execution engine *(Rupert Maiti)*

**What belongs here:** `AgentRunner` (the step loop), `RunConfig` (every
knob affecting a run, persisted for reproducibility), `RunResult`, and the
`ToolEnvironment` protocol (defined here, at the consumer, so environments
never import the runner). Also the batch layer — `BatchRunner` /
`BatchSummary` (`batch.py`), `run_task_pipeline` (`pipeline.py`), and the
per-batch `SuiteReport` (`report.py`).

**Exposes:** `AgentRunner(adapter, environment, artifact_store).run(task,
config) -> RunResult`; `build_initial_transcript` (prompt version `v0` —
bump `RunConfig.prompt_version` when it changes); `BatchRunner(store).run(suite)
-> BatchSummary`; `build_suite_report(summary, store) -> SuiteReport` +
`render_suite_report_markdown` (read-only roll-up of a finished batch's
on-disk artifacts — checks fired, failure categories, claimed-vs-observed
coverage; see [suite_report.md](suite_report.md)).

A suite may set `max_cost_usd` (`Suite 0.3.0`). `BatchRunner` asks
`BudgetGuard` before each run and stops the batch once the recorded spend of
its live runs reaches the cap, which the summary's `budget` block records as
`budget_exhausted` along with the cells never run (`BatchSummary 0.3.0`). A
live run of an unpriced model under a cap is refused before it starts, and a
live run that finishes with no recorded cost stops the batch after it; both are
recorded as `budget_unenforceable` and `run-suite` exits 2. Fixture and replay
runs cost exactly zero and are never refused on price. `branch` drives one
guard per invocation from the experiment plan's `max_cost_usd`, shared by every
condition and seed and started from what the experiment's earlier runs spent,
and stopped when an earlier stop left the cap unenforceable
([branch_stage.md](branch_stage.md#budget)). `run-sweep`
does not exist yet and is meant to drive the same `BudgetGuard`.

`branch.py` exposes `run_branch(artifact_path, experiment, condition, store)`
and `replay_batch(...)`, behind `trace-harness branch`. It continues a
regression artifact's recording from each experiment condition's start step
under that condition's agent and controls, verifies, attributes and bundles
each run the way `run_task_pipeline` does, records divergence from the
recording and the post-block outcome per entry, and writes one batch per
condition (`BatchSummary` 0.4.0). `experiment record` derives the divergence
rates and outcome counts from those batches. See
[branch_stage.md](branch_stage.md).

`target_agent.py` runs an outside agent (provider `external`). It exposes the
`TargetAgent` protocol, `TargetAgentBridge` (a model adapter that serves the
outside agent's tool calls and final answer to `AgentRunner` one step at a
time), `load_target_agent("package.module:factory")`, and `run_target_agent`.
The runner loop is unchanged for these runs, so step numbering, controls,
`blocked_by`, the final-answer seam, and the step and time limits behave as
they do for every adapter. The bridge sends no provider request, so no call
policy wraps it and `run_config.json` records `call_policy` as null. An error
from the outside agent ends the run as `model_error` and is never retried. The
budget guard refuses provider `external` under a cap as `budget_unenforceable`,
since its spend is invisible, and `branch` refuses it before any run. See
[bring_your_own_agent.md](bring_your_own_agent.md).

`collector.py` exposes `collect_regressions(path, store, suite_path=...,
experiments_path=...)` and `CollectorSummary` (`0.1.0`). It reuses replay's structured `ReplayReport` to gate
completed failure reproduction and positive siblings. Control validation gates
only for explicit `static_ok` labels; other labels remain advisory. The CLI and
`check_repo.sh` call this collector. See [the gate contract](regression_contract.md#what-ci-does).

**Rules (these are the architecture):** the runner never imports tool
implementations or a global tool registry; it contains zero scenario
knowledge (if a change mentions refunds, it belongs elsewhere); it never
runs verifiers (separate pipeline stage over artifacts); every run —
including crashes — leaves a run directory with `run_result.json`.
`status=completed` means "produced a final answer", never "was correct".
`build_suite_report` never re-runs a task and never writes — the CLI /
`ArtifactStore` own persistence — and degrades to category `unknown` + a
warning only when a run has a real violation and no attribution file; an
`incomplete` run (three-state verdict, see verifiers/ below) with no
violations gets no category and no warning, since there is nothing to
attribute.

**Build next:** a timeout that can interrupt a hung provider call (today
checked only between steps); multi-run orchestration (N runs, varied seeds) once live models make runs
non-deterministic; a TypeScript `SuiteReport` mirror for the dashboard in
the style of `apps/dashboard/src/data/run-loader.ts`.

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

**Build next:** structured citations in model actions; parent links for
provider responses, retries, and future sub-agent spans; storage backend
interfaces once local JSON demonstrably hurts.

## verifiers/ — deterministic pass/fail *(Karan Gupta)*

**What belongs here:** code that decides whether a finished run was
*actually correct*, returning structured `VerifierResult`s with evidence —
never bare booleans.

**Exposes:** `Verifier.verify(task, trace, final_state, run_id) ->
VerifierResult` (deterministic, side-effect free, robust to partial
traces); `VerifierResult` (with a three-state `verdict`: pass / fail / incomplete —
see verifier_philosophy.md) / `FailedCheck`/`EvidenceItem`; the
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

**Exposes:** `HeuristicAttributor.attribute(task, trace, verifier_result,
run_result=None) -> AttributionResult` (requires a *failed* verifier result;
raises on passed ones); `FailureCategory` (extend, never repurpose values);
`classify_post_block_outcome(trace, verifier_result, run_result)`
(`post_block.py`), which returns the first control block step and a
`PostBlockOutcome` label for any run, passed or failed (see
[failure_taxonomy.md](failure_taxonomy.md#post-block-outcome-labels)).

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

**Exposes:** `materialize_regression_artifact(...)` (schema 0.3.0, including
`replay_mode` and its recorded basis; see [regression contract](regression_contract.md));
`RegressionArtifact`/`SiblingTest`; `pinned_initial_state(...)` /
`describe_state_drift(...)` (`replay.py`) — the pinned world a
`trace-harness replay` run rebuilds, and how it differs from the fixture's
world today; `RepairValidation`/`ControlVerdict`/`decide_verdict(...)`
(`repair_validation.py`) — per-control accept/reject verdicts written as
`repair_validation.json`.

**Rules:** pin the run's *recorded* state, docs, and agent actions (snapshots
from the trace, not live fixtures — fixtures may evolve) and replay from
those, not from the files. Failed check ids are the assertion set.
`positive_sibling_tests` (from task metadata) are mandatory in spirit — a
fix that breaks the sibling is overblocking. This loop is TRACE's
differentiation over pure attribution (see
[AGENTRX_TRACE_SUMMARY.md](AGENTRX_TRACE_SUMMARY.md)).

`replay --apply-control` tests controls together and individually, recording
verdicts and replay evidence in `repair_validation.json`. `--control` limits
both stages; `--fail-on-rejected` gates individual rejections. Validation runs
share a `batch_id`. See [control validation](failure_bundles.md#control-validation)
for input handling, verdicts, and incomplete runs.

`replay --apply-control --commit` promotes accepted controls after replaying
the proposed library against new and existing regressions. The versioned
library retains evidence and rollback history. Environments and `run-suite`
load it explicitly with `control_library` / `--control-library`. See
[the lifecycle](failure_bundles.md#control-library) and
[ADR-0003](decisions/ADR-0003-control-library.md).

**Build next:** pinned sibling inputs
(today siblings run from their live fixtures).

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
