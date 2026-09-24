# docs/ — index

Read these in this order when you join; after that, use the "read before
you change X" column.

| Doc | Read it when |
|---|---|
| [terminology.md](terminology.md) | First day. The words we use, precisely — including the distinctions people blur (status vs verdict, mention vs reliance, root cause vs first irreversible action). |
| [architecture.md](architecture.md) | Before changing module boundaries, adding a dependency, or wiring anything to anything. Includes the observability stance. |
| [first_vertical_slice.md](first_vertical_slice.md) | Before touching the refund scenario — the staged failure's 7-step anatomy and what each pipeline stage produces. |
| [modules.md](modules.md) | Before your first PR. One section per `src/trace_harness/` module: what belongs there, the rules, what to build next. |
| [trace_schema.md](trace_schema.md) | Before adding/changing trace events or anything that reads `trace.jsonl`. |
| [methodology_metrics.md](methodology_metrics.md) | Before reporting any number, or adding one. Every metric with its formula, the artifact field it reads, and its blind spot. |
| [verifier_philosophy.md](verifier_philosophy.md) | Before writing pass/fail checks or proposing an LLM judge for anything release-blocking. |
| [attribution_methodology.md](attribution_methodology.md) | Before touching attribution — the step vocabulary and why its fields must never collapse. |
| [attribution_canonical_run_results.md](attribution_canonical_run_results.md) | Reviewing canonical attribution results, step meanings, evidence rules, and null handling. |
| [failure_bundles.md](failure_bundles.md) | Before changing cards, repair packages, or regression artifacts. |
| [team_ownership.md](team_ownership.md) | To find an owner, a reviewer, or your Linear workstream. |
| [future_api.md](future_api.md) / [future_dashboard.md](future_dashboard.md) | Picking up the API or dashboard — contracts and start conditions are pinned there. |
| [AGENTRX_TRACE_SUMMARY.md](AGENTRX_TRACE_SUMMARY.md) | Competitive context: why TRACE's differentiation is the reliability loop, not localization accuracy (Linear TRA-34). |
| [failure_taxonomy.md](failure_taxonomy.md) | Before adding a failure category or mapping a check to one. |
| [severity_policy.md](severity_policy.md) | Before setting or changing a check's severity, or deciding what blocks a release. |
| [regression_contract.md](regression_contract.md) | Before changing replay, its exit codes, or what a pinned artifact promises. |
| [task_validity.md](task_validity.md) | Before authoring a task fixture. The rubric `validate-fixtures` enforces. |
| [suite_report.md](suite_report.md) | Before changing the batch summary or the suite report. |
| [live_interface_compatibility_matrix.md](live_interface_compatibility_matrix.md) | Checking whether a contract is current, and what evidence on `main` backs it. |
| [canonical_documentations_and_decisionrecords.md](canonical_documentations_and_decisionrecords.md) | Looking for which document is canonical on a question. |
| [PROJECT_STATE_REVIEW_2026-07-28.md](PROJECT_STATE_REVIEW_2026-07-28.md) | Superseded. A record of what `main` looked like on July 28, 2026. |

## Acceptance records

Evidence kept for a claim someone will question later.

| Record | What it shows |
|---|---|
| [acceptance/2026-07-28-clean-checkout.md](acceptance/2026-07-28-clean-checkout.md) | A clean clone passing the full backend and dashboard matrix. |
| [acceptance/refund-v0-suite.md](acceptance/refund-v0-suite.md) | The canonical refund suite and its outcome counts. |
| [acceptance/failure-bundles-v0.md](acceptance/failure-bundles-v0.md) | The first five complete failure bundles. |
| [acceptance/live-gemini-2026-09-13/README.md](acceptance/live-gemini-2026-09-13/README.md) | Eight retained live Gemini runs, #179. |

## Plans and specs

Working documents kept for their reasoning. They are not contracts, and
where one disagrees with a doc above, the doc above wins.

| Document |
|---|
| [superpowers/plans/2026-07-28-canonical-five-outcome-suite.md](superpowers/plans/2026-07-28-canonical-five-outcome-suite.md) |
| [superpowers/plans/2026-07-28-dashboard-artifact-loader.md](superpowers/plans/2026-07-28-dashboard-artifact-loader.md) |
| [superpowers/plans/2026-07-28-integration-enforcement.md](superpowers/plans/2026-07-28-integration-enforcement.md) |
| [superpowers/specs/2026-07-28-integration-closure-topoff-design.md](superpowers/specs/2026-07-28-integration-closure-topoff-design.md) |

## Decisions (ADRs)

Hard-to-reverse decisions get a numbered ADR in [decisions/](decisions/).
Start with
[ADR-0001 — initial architecture](decisions/ADR-0001-initial-architecture.md):
why fixture-first, Pydantic contracts, local JSON artifacts,
deterministic-verifier-first, and no hosted services initially. Then
[ADR-0002 — Phase 3 and lanes](decisions/ADR-0002-phase3-replay-audit-and-lanes.md):
why the first Phase 3 experiment is the control replay-validity audit, why a
replayed control verdict is advisory until labeled, and why ownership moved
from named module owners to lanes. Then
[ADR-0003](decisions/ADR-0003-control-library.md) on the versioned control
library, and [ADR-0004](decisions/ADR-0004-brief-001-registration.md), which
registers the replay-validity audit as brief 001 and records why its sample
supports an existence test.

Write an ADR when reversing the decision later would be expensive (schema
contracts, storage layout, evaluation methodology, provider choices) or
when the same debate has happened twice. Format:
`ADR-NNNN-short-slug.md` with **Status / Context / Decision /
Consequences** — including the costs; an ADR without downsides is
advertising. Never rewrite an accepted ADR; supersede it. Anyone may
draft; the TPM lane reviews direction.

## Experiments

Research briefs live in [experiments/briefs/](experiments/briefs/). Each
brief's pre-registration lives in
[experiments/preregistration/](experiments/preregistration/) and merges
before any run it governs.

| Doc | Read it when |
|---|---|
| [Brief 001, control replay validity](experiments/briefs/001-control-replay-validity.md) | Before trusting a control verdict from `replay --apply-control`, or planning a live continuation run. |
| [Pre-registration 001](experiments/preregistration/001.md) | Before running or reporting any arm of brief 001. It fixes the hypotheses, sample, thresholds, and stopping rules. |

## Doc hygiene

Owned by the research and QA lane. Every code PR that changes behavior
described here updates the matching doc in the same PR, because a stale doc is
a bug report waiting to happen.

`tests/test_docs_index.py` fails when a doc is added without a link here, or
when a link here points at a doc that is gone. `tests/test_docs_versions.py`
fails when a current doc quotes a schema version that disagrees with its
constant in `src/`, but only for quotes in the forms it reads, such as
`TaskSpec X.Y.Z` or `TRACE_SCHEMA_VERSION = X.Y.Z`. It skips dated records
(acceptance records, ADRs, experiments, plans, the July 28 review) and notes
like "since TaskSpec X.Y.Z". It also fails when a schema constant is added to
`src/` without being mapped to a documented name or exempted.
