# Terminology

Use these terms consistently in code, docs, PRs, and Linear. When a word
here would be ambiguous in a sentence, prefer the precise field name from
the schema.

## Task
A structured test scenario for an agent: initial world state, available
tools and docs, what good behavior looks like, which verifiers judge it.
Schema: `trace_harness.tasks.schemas.TaskSpec`. Tasks *describe*; they
never execute.

## Target agent
The agent being evaluated. Today: a **scripted fixture agent**
(`FixtureModelAdapter` replaying a `FixtureScript`) and a placeholder
**Gemini adapter**. The harness treats both identically through the
`ModelAdapter` protocol — that's the point.

## Runner
The execution engine (`AgentRunner`): loads a task into an environment,
loops the model adapter, validates and executes tool calls, records the
trace, writes artifacts. Scenario-free by design.

## Environment
The sandboxed world the agent acts inside: tools (with declared
side-effect classes), mutable typed state, documents, and the side effects
the run produces. First instance: `SupportEnvironment` over
`SupportState`.

## Trace
The structured, append-only event log of what happened during a run
(`trace.jsonl`, one `TraceEvent` per line). The trace is evidence — it is
never edited after the fact.

## Verifier
Deterministic code that decides **pass/fail** for a finished run and
produces evidence (`VerifierResult` with `FailedCheck`s). The authority on
correctness. "The verifier decides pass/fail."

## Attribution / Judge
The component that explains **where and why** a verified failure happened
(`AttributionResult`: root cause step, missed recovery, first irreversible
action, categories, narrative). Today rule-based (`HeuristicAttributor`);
later LLM-assisted. "The judge explains why." A judge never overrides a
verifier's verdict.

## Failure card
Human-readable summary of one verified failure: what broke, where, how
bad, blast radius, evidence (`FailureCard`).

## Repair package
Structured engineering recommendation: concrete controls (each with an
installation point, deterministic check, behavior on failure, tradeoff,
priority) that would prevent the failure class (`RepairPackage`).

## Regression artifact
A rerunnable test derived from a failure: pinned state and docs, the
verifier checks that must hold, a replay command, and **positive sibling
tests** that must keep passing so the fix can't overblock
(`RegressionArtifact`).

## Useful distinctions people will otherwise blur

- **status vs verdict** — `RunResult.status=completed` means "produced a
  final answer", not "was correct". The verdict lives in
  `verifier_result.json`.
- **root cause vs first irreversible action** — where the failure began
  vs where it stopped being fixable. Different steps, different fields
  (refund fixture: 3 vs 5).
- **deprecated-doc mention vs reliance** — reading or naming a stale doc
  is fine; citing it as the basis for a violating action is the failure.
- **staged failure vs measured failure** — a fixture script failing is
  choreography for building the harness; only live-model runs measure an
  actual agent.
