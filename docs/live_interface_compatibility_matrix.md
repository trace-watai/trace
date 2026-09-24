# TRACE live interface compatibility matrix

Date: 2026-07-28
Linear anchor: TRA-66
Owner: Justin Lam
Verified baseline: `origin/main` through PR #123 integration

## Executive result

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

## Contract matrix

| Interface or artifact | Producer and version | Current consumers | Evidence on `main` | Status / remaining gap |
| --- | --- | --- | --- | --- |
| Task definition | `TaskSpec 0.5.0` | loader, runner, verifier, regression | Escalation is a first-class field; validation rejects escalation tasks without the escalation tool. | Compatible. Several task-family directories are still placeholders rather than runnable coverage. |
| Support state | `State 0.2.0` | environment, verifier, failure bundle, regression | Escalations have structured state, sequence IDs, traceable creation steps, and final-state persistence. | Compatible. |
| Tool surface | Environment registry | fixture and future live agents | `search_docs`, `get_order`, `issue_refund`, `create_ticket`, and `escalate_case` share validation, tracing, side-effect labels, and hook behavior. | Compatible. |
| Trace | `TraceEvent 0.5.0` | verifier, attribution, dashboard, regression evidence | Backend and TypeScript event vocabularies align; parent event links and structured payloads are tested. | Compatible. A live provider still needs to prove safe `model_response` capture and redaction. |
| Run config/result | `RunConfig 0.2.0`, `RunResult 0.1.0` | CLI, index, reader, batch, dashboard fixture | Each child run retains a normal run directory and effective configuration, including optional cassette settings. | Compatible; `0.1.0` configs remain readable. |
| Run index/read path | `RunIndex 0.5.0`, `RunReader` | CLI, batch reporting, future API/dashboard | Verification enriches the canonical index; list-runs shows PASS/FAIL; terminated runs stay distinct. | Compatible. The dashboard reads the index through `run-loader.ts` and lists every retained run. |
| Verifier result | `VerifierResult 0.4.0` | failure bundle, regression, dashboard | Backend and TypeScript align, including escalation-record evidence, missing-escalation checks, and the three-state `verdict` (pass, fail, incomplete). | Compatible. |
| Attribution | `AttributionResult 0.3.0` | failure-card generator, audit consumers | Heuristic attribution is validated against trace/evidence and preserves ambiguity notes. | Compatible for the current refund vertical. |
| Failure card | `FailureCard 0.4.0` | visible dashboard card, offline fixture | Backend and TypeScript align on structured blast radius, including escalation count. | Compatible and visibly rendered, with multi-run navigation from #149 and the trace timeline from #150. |
| Repair package | `RepairPackage 0.3.0` | dashboard-ready contract, humans, regression planning | Controls are linked to actual verifier checks and have priority, location, behavior, impact, and tradeoffs. | Compatible. Not yet rendered as a complete dashboard section. |
| Regression artifact | `RegressionArtifact 0.3.0` | replay CLI, dashboard contract | Pinned state, docs, normalized agent actions, verifier checks, positive siblings, and control replay are tested. | Compatible and executable. |
| Suite config/summary | `Suite 0.2.0`, `BatchSummary 0.2.0` | CLI, delivery reporting | Child run IDs, verdict counts, termination counts, cost coverage, and canonical artifacts are emitted and tested. | Compatible; `0.1.0` suites remain readable. Unknown live-provider cost remains `null` rather than being reported as zero. |
| Full offline run fixture | Deterministic 11-artifact bundle | dashboard tests and offline demo | Generated from the current pipeline; all run IDs and artifact links align; regeneration is byte-for-byte deterministic. | Compatible and current. |
| Dashboard contracts/UI | TypeScript mirrors listed above | browser UI | Format, lint, typecheck, 57 tests, and production build pass. A run list, a run detail page and a trace timeline with attribution markers and per-step verifier failures all render from retained runs. | Partial product surface. Repair package and regression artifact panels and a real backend connection remain, the last two tracked by #205. |
| Gemini/live adapter | `src/trace_harness/models/gemini.py` | CLI and suite runner | Native function calling is implemented with current `google-genai`; the shut-down Gemini 2.0 default was replaced with `gemini-3.6-flash`; suite temperature, seed, and timeout reach both the persisted config and adapter; provider responses precede normalized actions in the trace; parallel calls fail explicitly. | Contract-compatible, offline-tested, and proved live. #179 retained eight key-backed runs under `docs/acceptance/live-gemini-2026-09-13/`. Retries, backoff and cost extraction remain follow-ups. |

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

## Current acceptance evidence

Fresh verification on the merged stack:

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

## Remaining release gates and owners

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
