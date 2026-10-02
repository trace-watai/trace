# Brief 001. Control replay validity

Issue #158 (Linear TRA-96). Hypotheses, sample, and decision rules are
registered in [preregistration/001.md](../preregistration/001.md). ADR-0002
(decision 1) chose this audit as Phase 3's first brief, and
[ADR-0004](../../decisions/ADR-0004-brief-001-registration.md) records its
registration.

## Goal

A control's static replay verdict should count as unverified until live
continuation after the block agrees with it, and brief 001 measures that
agreement. Every control verdict in the library comes from
`replay --apply-control`, which re-runs a recording with the control
installed. A recording cannot react to being blocked, which is why ADR-0002
(decision 2) treats these verdicts as advisory.

The surrounding loop is published. HarnessFix (arXiv 2606.06324, June 2026)
attributes agent failures to harness artifacts, synthesizes patches, and
accepts them under regression-aware validation. Harness-R1 (arXiv 2608.02276,
August 2026) learns failure-conditioned edits to lifecycle hooks, including a
pre-action guardrail, and rewards them by re-running every task, passing ones
included. Phantom Guardrails (arXiv 2607.13083, July 2026) shows a
self-improving harness fabricating a guardrail for a failure that never
happened in 15 of 60 runs when the input resembled a familiar rule, against 0
of 60 without that resemblance, and shows acceptance based on suppression
alone cannot see it.
Cautious Bench (arXiv 2608.27009, August 2026) ships 756 benign and twin pairs
for guard over-blocking. LlamaFirewall (arXiv 2505.03574) derives a threshold
by fixing utility loss and reports both sides on AgentDojo. The Verifier Tax
(arXiv 2603.19328) measures the tradeoff between blocking and recovery on
tau-bench. Step-level failure attribution remains weak, at 11% best reported
accuracy on TRAIL (arXiv 2505.08638) and 14.2% in Zhang et al. (ICML 2025,
arXiv 2505.00212). Three August 2026 papers in issue #28's matrix (the Replay
Gap, verdict-staleness, and influence-versus-authority papers) are the
closest neighbors. ADR-0002 summarizes that matrix as measuring the
static-versus-live question for model switching, for guardrail verdicts in
control systems, and for full-path authorization.

None of this work measures whether a static replay verdict on a control
agrees with a live agent continuing a pinned trajectory after that control
blocks it. That agreement is brief 001's contribution, and the loop itself is
prior art.

## Scope

The environment is the refund and support workflow. Fork points are the
failing runs of `fixtures/suites/refund_v0.json` and
`fixtures/suites/refund_bundles_v0.json` that record a control block. There
are 3, from 2 task families, and all are labeled `live_required`. At that n
the brief can show that disagreement exists and cannot estimate its rate. The
pre-registration gives the counts and the arithmetic.

refund_v0 is harness verification under a scripted agent
(`docs/acceptance/refund-v0-suite.md`). Its 18 passes and 14 failures are
staged by fixture scripts. This brief draws fork points from its failures
and reports none of its pass rates as model coverage.

The `static_replay` arm runs the fixture adapter
(`src/trace_harness/models/fixture.py`), which ignores the transcript. After a
block it plays the next recorded action regardless, so a scripted replay
cannot re-plan. That limitation is the gap under measurement and the reason
ADR-0002 (decision 2) treats static verdicts as advisory.

The deterministic verifier judges every arm (`docs/verifier_philosophy.md`).
LLM judges, a second domain, and judge-prompt or retrieval-config experiments
are out of scope.

## Target failure modes

| Failure mode | What a live agent does after the block | Evidence |
|---|---|---|
| Substitute violation | Reaches a forbidden outcome by another path, such as store credit once the cash refund is blocked | A blocking failure after the fork on a check the control leaves uncovered. `issue_refund` reaches `unauthorized_store_credit`, which the refund window control does not cover |
| False success | Reports the blocked action as done | `final_answer_inconsistent_with_state` after the fork |
| Recovery the script cannot show | Declines or escalates correctly while the recorded continuation keeps failing | A clear live verdict against a failing static verdict |
| Over-blocking | A positive sibling fails with the control installed | `sibling_failure_rate` |

## Frozen evaluation

These paths are fixed from registration to the report. A change to any of
them after the first live run voids the comparison.

| Path | Why it is frozen |
|---|---|
| `fixtures/suites/refund_v0.json` | Source of fork points |
| `fixtures/suites/refund_bundles_v0.json` | Source of fork points |
| `fixtures/tasks/` and `fixtures/scripts/` | The tasks and recorded actions the fork points replay |
| `fixtures/expected/` | Pinned verifier outcomes and replay-mode labels |
| `src/trace_harness/verifiers/` | The judge |
| `src/trace_harness/environment/` | The sandbox, apart from the new registry entries allowed below |
| `scripts/check_repo.sh` | The gate every commit in the experiment passes |

## Allowed changes

Two things may change. The first is new controls registry entries, meaning
guardrail entries in `src/trace_harness/environment/controls.py`, the
guardrails they name in `src/trace_harness/environment/guardrails.py`, and
control instances in `fixtures/controls/library.json`. Existing entries stay
as registered. A new entry changes which runs record a block, so it counts as
confirmatory only through a dated amendment to the pre-registration made
before the first live run. The second is the arm configuration (models,
seeds, temperature, each arm's control set, and budget) within the bounds the
pre-registration fixes. A configuration change after the first live run
starts a new experiment, and its results are exploratory.

## Metrics

Names come from the appendix of `docs/methodology_metrics.md` and formulas
from its Part B2. No combined score is computed.

| Metric | Use in this brief |
|---|---|
| `verdict_agreement_rate` | Primary, for H1 and H3, per model, with the per-seed share beside each majority verdict |
| `first_post_fork_divergence_rate` | Live control-on arms, read against the noise floor |
| `noise_floor_divergence_rate` | The `live_no_control` arm |
| `post_block_outcomes` | H2, with `no_block_observed` runs counted separately |
| `sibling_failure_rate` | Over-blocking tripwire on the experiment's own conditions |
| `verified_failure_count` | Per arm |
| `cost_usd` | Per arm, with recorded runs over total runs |
| `latency_ms_p50` | Per arm and per model |

Every rate is reported as k / n with its one-sided 95% Clopper-Pearson bound.
The experiment record will use the #155 contract once it merges, and until
then values come from per-run `verifier_result.json`, the batch summaries, and
the suite report (`docs/suite_report.md`). The divergence rates need the
branch stage (#159) and `post_block_outcomes` needs the post-block outcome
classifier (#157), so no live arm runs before #159 merges.

## Experiment rule

The pre-registration merges before any live arm runs and before anyone reads
a static verdict, and it binds the analysis. The deterministic verifier's
verdict on each run is final, and no run is rescored by hand or by a model.
Each arm configuration is one experiment record. Analysis the
pre-registration does not name is labeled exploratory, with the number of
variants tried. The brief ends in a measurement, so no control is kept or
discarded on its result.

## Reporting

The report is a new file under `docs/experiments/`, committed with its run
evidence. It opens with every deviation from the pre-registration, then
reports each hypothesis as supported, refuted, null, or not tested, per model
and per family, with k / n and bounds. It lists arms that did not run and the
15 failing tasks that record no block, each with the reason. The
pre-registration's list of claims we will not make binds its wording.
