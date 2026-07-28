   # Failure bundles: cards, repair packages, regression artifacts

When a verified failure exists, TRACE converts it into three artifacts —
generated together by `FailureBundleGenerator`, stored in the run
directory, consumed by humans, CI, and the dashboard. Owner: Samir
Mohammed.

## Failure card (`failure_card.json`) — for humans

The one-pager a teammate reads to understand the failure without opening
the trace: title, summary, `task_result` (run status *and* verdict —
distinct on purpose), severity, root cause (quoting the actual reasoning
at the root-cause step), visible symptoms, evidence (carried from verifier
checks, step-linked), causal explanation (from attribution), and **blast
radius**.

Blast radius is *computed from final state* — dollars out, durable records
created, customers affected ("1 refund totalling $432.00; 1 durable ticket
record; 1 customer") — never adjectives. Scope you can verify beats
"severe impact" you can't.

## Repair package (`repair_package.json`) — for engineers

Concrete controls that would prevent the failure class. Controls are
selected by which verifier checks failed (check id → control template), so
the package never prescribes fixes for failures that didn't happen. Every
control must name:

- `installation_point` — a real seam in this codebase or CI (e.g.
  "`SupportEnvironment.execute`, pre-dispatch for `issue_refund`" — the
  comment marking that seam exists in the code);
- `check` — the deterministic test the control performs;
- `behavior_on_failure` — block/escalate/correct, specifically;
- `why_it_prevents_recurrence` — the causal claim;
- `risk_or_tradeoff` — what it might overblock or complicate (a control
  with "no tradeoffs" hasn't been thought through);
- `priority` (P0–P3) and `linked_verifier_checks`.

For the refund failure: deterministic pre-call refund guardrail (P0),
current-policy source precedence (P1), ticket claim-grounding (P1), and —
always — the regression CI gate (P0). Note the deliberate redundancy:
the guardrail stops the harm even if source precedence fails again.
Defense in depth, not a single fix.

## Regression artifact (`regression_artifact.json`) — for CI

The failure, pinned and rerunnable: initial state, docs, and the agent's
actions *as the failing run saw and did them* (snapshots, not live fixtures —
fixtures may evolve), the failed check ids as the assertion set, severity,
`blocks_release`, a replay command, and **positive sibling tests**.

Positive siblings are the anti-overblocking mechanism and they are
mandatory in spirit: a fix for "unauthorized refund at 47 days" that also
blocks the legitimate 12-day refund is a new bug. The sibling
(`refund_policy_valid_cash`) must keep passing in the same CI gate that
replays the failure.

Two ways to rerun it, and they are not equivalent: the `replay_command`
field is a plain `run-pipeline` on the originating fixture (whatever that
fixture says *today*), while `trace-harness replay <artifact>` rebuilds the
world from the pinned state and asserts the gate conditions. Prefer the
latter in CI — see [regression_contract.md](regression_contract.md).

## Generation rules

1. **No fake intelligence.** MVP output is template-assembled from
   deterministic signals. When LLM assistance lands, it must cite the same
   evidence, and deterministic fields remain.
2. **Failures only.** The generator raises on passing runs; the CLI skips
   bundle generation when the verifier passes. No artifacts without a
   verified failure behind them.
3. **Evidence chains end at steps.** Card → checks → evidence → step ids →
   trace events. The dashboard renders this chain; breaking it breaks the
   product story.

## Lifecycle (the part that out-positions pure attribution)

```
verified failure → bundle → human review → control installed →
regression replayed in CI (with siblings) → release gate → trendline
```

This loop — not localization accuracy — is TRACE's differentiation (see
AGENTRX_TRACE_SUMMARY.md). The bundle generator is where a one-time
finding becomes a permanent test.

---

## Field reference

### `FailureCard` fields

| Field | Type | Required | Source | Description |
|---|---|---|---|---|
| `schema_version` | `str` | auto | hardcoded | Schema version; bump when fields are added or removed (currently `0.2.0`) |
| `run_id` | `str` | yes | runner | Unique ID of the run that produced this failure |
| `task_id` | `str` | yes | task spec | ID of the task that was attempted |
| `title` | `str` | yes | generated | Short headline: task title + first failed check message |
| `summary` | `str` | yes | generated | One-paragraph summary: run status, steps taken, which checks failed |
| `task_result` | `str` | yes | generated | Run outcome and verifier verdict combined, e.g. `"completed (final_answer, 7 steps); verifier FAILED (3 checks)"`. Run status and verifier verdict are kept separate on purpose — a run can complete successfully and still fail verification |
| `severity` | `Severity` | yes | verifier | Highest severity among failed checks (`low`, `medium`, `high`, `critical`) |
| `root_cause` | `str` | yes | attribution + trace | Step number and failure category where the failure began; quotes the agent's reasoning at that step when available |
| `contributing_failures` | `list[str]` | no (defaults `[]`) | attribution | Failure categories that contributed, primary first — e.g. `["stale_source_authority", "unsafe_irreversible_action"]`. Populated from `AttributionResult.primary_failure_category` and `contributing_failure_categories` |
| `step_ids` | `list[int]` | no (defaults `[]`) | verifier checks | Sorted list of step numbers directly implicated in the failure, drawn from the union of all failed check `step_ids`. Lets a reader jump straight to the relevant trace lines |
| `visible_symptoms` | `list[str]` | no (defaults `[]`) | verifier checks | Human-readable message from each failed check — what was observable wrong |
| `evidence` | `list[EvidenceItem]` | no (defaults `[]`) | verifier checks | Structured evidence items from failed checks plus any run-level evidence; each item carries `kind`, `description`, `step_ids`, and raw `data` |
| `causal_explanation` | `str` | yes | attribution | Narrative explanation of why the failure happened, sourced directly from `AttributionResult.causal_explanation` |
| `blast_radius` | `str` | yes | final state | Computed scope of external impact: dollars refunded, durable records created, customers affected. Always a measurable statement, never an adjective |
| `metadata` | `dict` | no (defaults `{}`) | generated | Supplementary data: `primary_failure_category` and `attribution_confidence` |

### `RepairControl` fields

Every control in a repair package must fully specify all required fields. A control that cannot name its installation seam or its tradeoff is not ready to ship.

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | `str` | yes | Machine-readable identifier for the control, e.g. `deterministic_pre_call_refund_guardrail` |
| `installation_point` | `str` | yes | Exact location in the codebase or CI where the control installs — a real seam, not a vague layer. Must reference an actual file, class, or method |
| `check` | `str` | yes | The deterministic test the control performs, stated precisely enough that an engineer can implement it without ambiguity |
| `behavior_on_failure` | `str` | yes | What happens when the check fails: block, escalate, correct, or flag — stated specifically |
| `expected_impact` | `str` | yes | The observable engineering outcome if the control is installed: which verifier check(s) stop firing, which failure class is eliminated. This is the measurable result, not the causal explanation |
| `why_it_prevents_recurrence` | `str` | yes | The causal claim: why installing this control structurally prevents the failure from happening again, regardless of model or prompt variation |
| `risk_or_tradeoff` | `str` | yes | What the control might overblock, complicate, or break. A control with no tradeoffs has not been thought through |
| `priority` | `str` | yes | See control priority ranking below |
| `linked_verifier_checks` | `list[str]` | no (defaults `[]`) | The check IDs from the verifier result that this control addresses |

### `RepairPackage` fields

| Field | Type | Required | Description |
|---|---|---|---|
| `schema_version` | `str` | auto | Currently `0.2.0` |
| `run_id` | `str` | yes | Run that produced this package |
| `task_id` | `str` | yes | Task that was attempted |
| `summary` | `str` | yes | How many controls, which checks they address, overall severity |
| `controls` | `list[RepairControl]` | no (defaults `[]`) | Ordered list of controls; regression CI gate is always last |
| `metadata` | `dict` | no (defaults `{}`) | `generated_from_checks`: the deduped list of failed check IDs that drove control selection |

---

## Control priority ranking

Controls are ranked P0–P3 based on urgency and scope of harm prevented.

| Priority | Meaning | When to use |
|---|---|---|
| **P0** | Do before the next release | Prevents money moving, durable records being written incorrectly, or user-facing harm. Also applies to the regression CI gate — locking in the test is always P0 |
| **P1** | Do in the current sprint | Prevents a verified failure class from recurring but doesn't stop active harm (e.g. retrieval ranking fixes, prompt contract changes) |
| **P2** | Schedule soon | Reduces risk or improves robustness but the failure class requires multiple conditions to trigger |
| **P3** | Opportunistic | Nice-to-have hardening; address when refactoring the relevant area |

Controls addressing the same root cause are ordered with the most defensive first. The regression CI gate (`regression_test_ci_gate`) is always included and always last — it is the safety net that catches any failure that slips past the other controls.

---

## Verifier and attribution → card/package field mapping

| Source field | Lands in |
|---|---|
| `VerifierResult.severity` | `FailureCard.severity`, `RepairPackage.summary` |
| `VerifierResult.failed_checks[*].message` | `FailureCard.visible_symptoms`, `FailureCard.title` (first check) |
| `VerifierResult.failed_checks[*].evidence` | `FailureCard.evidence` |
| `VerifierResult.failed_checks[*].step_ids` | `FailureCard.step_ids` (union, sorted) |
| `VerifierResult.failed_checks[*].check_id` | `RepairControl.linked_verifier_checks`; drives which controls are generated via `_CONTROL_BUILDERS` |
| `AttributionResult.primary_failure_category` | `FailureCard.contributing_failures[0]`, `FailureCard.metadata` |
| `AttributionResult.contributing_failure_categories` | `FailureCard.contributing_failures[1:]` |
| `AttributionResult.causal_explanation` | `FailureCard.causal_explanation` |
| `AttributionResult.root_cause_step` | `FailureCard.root_cause` (step number + reasoning quote) |
| `RunResult.status` / `termination_reason` / `steps_taken` | `FailureCard.task_result`, `FailureCard.summary` |
| `final_state` (parsed as `SupportState`) | `FailureCard.blast_radius` |
