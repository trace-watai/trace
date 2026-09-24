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

## One card per root cause

A sweep over two providers and many seeds repeats one failure many times.
Since failure card 0.5.0 the bundle stage writes one card per root cause and
records every later run that repeated it as an occurrence of that card (#211).

### How a key is formed

Three facts about the failed run form the key, and nothing else does.

1. The failed verifier check ids, deduplicated and sorted.
2. The primary failure category from attribution.
3. The tool the run called at its first irreversible step, or no tool when
   the run took no irreversible action.

The first irreversible step is `AttributionResult.first_irreversible_action_step`,
the field the regression artifact's replay basis also pins. The attributor sets
it from the first `tool_call_executed` event whose `side_effect` is
`external_irreversible` and whose status is `ok`, and the key reads the tool
name off that same event.

The key is a hash behind a readable prefix.

```text
v1:<primary category>:<tool or none>:<digest>
v1:unsafe_irreversible_action:issue_refund:e621a26e69c17400
```

The digest is the first 16 hex characters of SHA-256 over this JSON, written
with sorted keys and no whitespace.

```json
{"category":"unsafe_irreversible_action","checks":["unauthorized_cash_refund"],"tool":"issue_refund","version":"v1"}
```

The prefix makes an index or a card readable at a glance. Keys are compared
whole, and the digest is what separates two failures that share a category
and a tool but fired different checks. With no irreversible step the JSON
records `"tool":null` and the prefix reads `none`. Run ids, task ids, step
numbers, messages and evidence stay out of the key, so one failure keys the
same way across tasks, providers, seeds and days. Changing any of the three
facts, or how they are written, means bumping `v1`. `tests/test_bundle_dedup.py`
pins the key of every failing task fixture so such a change shows up as an
edit to that table.

### Where the card lives

`bundle` looks the key up in the run index (`RunIndexEntry.bundle_key`, run
index 0.6.0) while holding a lock on `.bundle.lock` in the runs directory, so
two bundle stages writing into one directory see each other's cards.

- **No card has the key.** The bundle is written to the run's own directory
  as it always was, and the card lists the run as its first and only
  occurrence.
- **A card in another run's directory has the key.** The run is appended to
  that card's `occurrences`, and its own directory gets `bundle_ref.json` in
  place of a card, repair package and regression artifact. The pointer
  records the key and the `canonical_run_id` whose directory holds the card.

The card, the repair package and the regression artifact stay pinned to the
first occurrence. The card's text, evidence and blast radius describe that
run, and `occurrences` lists every run that repeated it along with the
provider, model and seed from each run's `run_config.json`.
`RunReader.get_bundle` and the dashboard serve a reproduction the card it
joined, whose `run_id` names the first occurrence.

The lookup is scoped to one runs directory, and two different tasks that fail
the same way share a card when their runs land in the same one. Of the
nineteen failing task fixtures, nine fall into four groups that share a key.
Neither `refund_v0` nor `refund_bundles_v0` runs two tasks from one group, so
every pinned suite expectation still has one card per failing task.

### Bundling a run again

Bundling a run again never counts it twice. A reproduction already on the
card keeps its place, and the card's own run keeps its occurrences. A run
whose key changed since it was last bundled leaves its old card's
occurrences. A run holding a card that other runs point to refuses a new key
with `BundleKeyConflictError`, because moving that card would leave their
pointers naming the wrong failure.

The index entry is written before any file, and the lookup only accepts a run
whose card file carries the key. An interrupted bundle therefore leaves an
entry that resolves to nothing, the next run with that key starts a card, and
bundling the interrupted run again makes it a reproduction of that card.

### Cards written before 0.5.0

Older cards load with `bundle_key` null and an empty `occurrences` list,
meaning the card describes its own run alone. They are never matched by key,
so retained runs keep the identity they were written with. The two reference
outside-agent runs under `docs/acceptance/runs/reference-agents-scripted-2026-09-23/`
repeat one failure and keep their two cards for that reason. Bundling such a
run again is the explicit step that brings it under a key. When a card with
that key already exists elsewhere, the run becomes one of its reproductions
and its own bundle files give way to the pointer. An index written before 0.6.0
is rebuilt on first read, recovering keys from cards and pointers.

### Replay and the regression gate

A regression artifact is never rewritten once written, so `replay`, the
control library's evidence hashes and the collector's `source_sha256` see the
same bytes however many reproductions accrue. `collect-regressions` discovers
`regression_artifact.json` files, which only first occurrences hold, so each
key is replayed once and reproductions are not counted in `artifacts_found`.
A failed run from the collector's optional suite passes the coverage check
when its pointer names a run holding an artifact, and the suite report
credits that row with the first occurrence's `regression_test_name`. The cost
is that a reproduction from a different task is replayed only through the
first task's pinned world. Similarity beyond the key and merging across
domains are outside #211.

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
| `schema_version` | `str` | auto | hardcoded | Schema version; bump when fields are added or removed (currently `0.5.0`) |
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
| `bundle_key` | `str \| null` | no (defaults `null`) | generator | Root-cause identity, formed as described in [How a key is formed](#how-a-key-is-formed). Null on cards written before `0.5.0` |
| `occurrences` | `list[BundleOccurrence]` | no (defaults `[]`) | bundle stage | Every run the card covers in the order they were bundled, the card's own run first. Each entry has `run_id`, `task_id`, `provider`, `model` and `seed`. Empty on cards written before `0.5.0` |

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

### Prescribed control ↔ executable control

A repair package *prescribes* controls by name. Whether one can actually be
installed is recorded in `environment/controls.py::MATERIALIZABLE_REPAIR_CONTROLS`,
which maps each prescribed `RepairControl.name` to the `guardrail_ref` that
implements it, or to `None` when nothing does yet. Per-control validation
reports the latter as `skipped: not_materializable` rather than pretending,
and writes every verdict to `repair_validation.json` (issue #146).

| Prescribed `RepairControl.name` | Executable `guardrail_ref` |
|---|---|
| `deterministic_pre_call_refund_guardrail` | `unauthorized_cash_refund_guardrail` (installed by `ctl_refund_window_v1`) |
| `current_policy_source_precedence` | none yet |
| `ticket_claim_grounding_check` | none yet |
| `final_answer_state_grounding_check` | none yet |
| `required_escalation_enforcement` | none yet |
| `escalation_discipline_check` | none yet |
| `retrieval_before_action_check` | none yet |
| `expected_action_contract_check` | never; detection only, see below |
| `regression_test_ci_gate` | never; a CI-side control, #161 makes it real |

Two of these will never have a `guardrail_ref`, and saying so is the point.
`expected_action_contract_check` covers a remedy that was omitted or swapped,
and a pre-dispatch hook can only stop an action, never cause one, so blocking
would make an omitted refund look fixed while the customer still has nothing.
`regression_test_ci_gate` runs in CI rather than in the environment. Both are
prescribed honestly and reported as `skipped: not_materializable` rather than
counted as coverage.

Every check id the verifier can emit has a template, enforced by
`tests/test_control_templates_lockstep.py`. Adding a check without a severity
or a template fails that test rather than producing a bundle with a gap nobody
notices.

### Control validation

`replay --apply-control` writes `repair_validation.json` at schema `0.1.0`
under `<output-runs-dir>/<source_run_id>/`, reading prescriptions beside the
input regression artifact. Invalid or mismatched packages fail before replay;
empty packages produce no verdicts. Without a package, selected reference
controls use all pinned checks and record `controls_source: reference_controls`.

Each verdict includes control identity, reason, originating and sibling run
IDs, failed checks, and linked checks cleared on completed replays. Evidence
retains `PASS`, `FAIL`, or `INCOMPLETE`; a rollup counts the control verdicts.

| Verdict | Condition |
|---|---|
| `accepted` | Linked checks cleared, no new blocking check appeared, and all declared siblings passed. |
| `rejected_failure_persists` | A linked check still fired. |
| `rejected_overblocks` | Linked checks cleared, but a new blocking check appeared or a positive sibling failed. |
| `skipped` | `not_materializable`, `not_selected`, `no_linked_checks`, or `validation_incomplete`. |

Incomplete evidence takes precedence and always causes exit 1. Other skips
do not fail the gate. `--fail-on-rejected` also gates individual rejections.
Invalid inputs return exit 2.

`inspect <source_run_id>` renders validation even without a local trace.
`list-runs --batch <batch_id>` groups individual validation runs.

`refund_policy_control_demo` earns acceptance. `refund_policy_failure` remains
rejected because its script claims a blocked refund, introducing
`final_answer_inconsistent_with_state`. Live-agent recovery needs separate validation.

### Control library

```text
failure → card → repair → validation → library → regression CI gate
```

`replay --apply-control --commit` promotes accepted controls into a versioned
library. It replays the proposed active set against each new and existing
originating regression and its positive siblings before writing. The library
retains source, validation, and activation evidence with relative paths and
SHA-256 hashes. Missing, changed, or mismatched evidence prevents loading.

```bash
trace-harness replay runs/<run_id>/regression_artifact.json --apply-control --commit --control-library runs/local-controls/library.json
trace-harness run-suite fixtures/suites/refund_v0.json --control-library runs/local-controls/library.json
trace-harness controls rollback ctl_refund_window_v1 --reason "restore baseline" --control-library runs/local-controls/library.json
```

Library loading is explicit. Active controls install in sorted ID order;
each batch uses one validated snapshot. Plain validation leaves the library
unchanged. Rollback appends a reason and preserves evidence; reusing an ID
with existing history is rejected. These operations write local artifacts,
not Git commits.

The [retained example](../fixtures/controls/README.md) preserves all 18
passing suite cases. Two negatives lose `unauthorized_cash_refund` but retain
false refund claims, so they still fail. Separate controlled expectations
record those outcomes. The full CI collector remains #161; static replay
remains advisory for live-agent recovery under ADR-0002.

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
