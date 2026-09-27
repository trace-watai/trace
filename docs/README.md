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
| [experiment_contract.md](experiment_contract.md) | Before running or recording an experiment. What the plan freezes, what the result records, and why there is no combined score. |
| [verifier_philosophy.md](verifier_philosophy.md) | Before writing pass/fail checks or proposing an LLM judge for anything release-blocking. |
| [attribution_methodology.md](attribution_methodology.md) | Before touching attribution — the step vocabulary and why its fields must never collapse. |
| [attribution_canonical_run_results.md](attribution_canonical_run_results.md) | Reviewing canonical attribution results, step meanings, evidence rules, and null handling. |
| [failure_bundles.md](failure_bundles.md) | Before changing cards, repair packages, or regression artifacts. |
| [team_ownership.md](team_ownership.md) | To find an owner, a reviewer, or your Linear workstream. |
| [future_api.md](future_api.md) / [future_dashboard.md](future_dashboard.md) | Picking up the API or dashboard — contracts and start conditions are pinned there. |
| [AGENTRX_TRACE_SUMMARY.md](AGENTRX_TRACE_SUMMARY.md) | Competitive context: why TRACE's differentiation is the reliability loop, not localization accuracy (Linear TRA-34). |

## Decisions (ADRs)

Hard-to-reverse decisions get a numbered ADR in [decisions/](decisions/).
Start with
[ADR-0001 — initial architecture](decisions/ADR-0001-initial-architecture.md):
why fixture-first, Pydantic contracts, local JSON artifacts,
deterministic-verifier-first, and no hosted services initially. Then
[ADR-0002 — Phase 3 and lanes](decisions/ADR-0002-phase3-replay-audit-and-lanes.md):
why the first Phase 3 experiment is the control replay-validity audit, why a
replayed control verdict is advisory until labeled, and why ownership moved
from named module owners to lanes.
[ADR-0004](decisions/ADR-0004-brief-001-registration.md) registers that
audit as brief 001 and records why its sample supports an existence test.

Write an ADR when reversing the decision later would be expensive (schema
contracts, storage layout, evaluation methodology, provider choices) or
when the same debate has happened twice. Format:
`ADR-NNNN-short-slug.md` with **Status / Context / Decision /
Consequences** — including the costs; an ADR without downsides is
advertising. Never rewrite an accepted ADR; supersede it. Anyone may
draft; Mohammed reviews direction.

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

Owners: Katharine (docs quality), Justin (methodology docs). Every code PR
that changes behavior described here updates the matching doc in the same
PR — stale docs are bug reports waiting to happen.
