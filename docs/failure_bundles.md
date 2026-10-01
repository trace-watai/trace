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
v2:<primary category>:<tool or none>:<digest>
v2:unsafe_irreversible_action:issue_refund:23b3db1014dba147
```

The digest is the first 16 hex characters of SHA-256 over this JSON, written
with sorted keys and no whitespace.

```json
{"category":"unsafe_irreversible_action","checks":["unauthorized_cash_refund"],"task":"refund_policy_control_demo","tool":"issue_refund","version":"v2"}
```

The prefix makes an index or a card readable at a glance. Keys are compared
whole, and the digest is what separates two failures that share a category
and a tool but fired different checks. With no irreversible step the JSON
records `"tool":null` and the prefix reads `none`. The task id is in the key,
and run ids, step numbers, messages and evidence stay out, so runs of one task
that fail the same way key the same across providers, seeds and days, and two
tasks never share a key. Each task therefore keeps its own card and regression
artifact to replay or branch from, even beside another task that fails the
same way. Changing any of the four facts, or how they are written, means
bumping `v2`. Keys written as `v1`, before the task was part of the key, never
equal a `v2` key, so a run bundled now never joins a `v1` card. `tests/test_bundle_key.py`
pins the key of every failing task fixture so such a change shows up as an
edit to that table.

### Where the card lives

`bundle` looks the key up while holding a lock on `.bundle.lock` in the runs
directory, so two bundle stages writing into one directory see each other's
cards. The run index (`RunIndexEntry.bundle_key`, run index 0.6.0) nominates
candidates. The index is a derived file that can lack a key a card has, so on
a miss the stage scans every `failure_card.json` in the directory for the key
before it writes a new card, and writes a key found that way back to the
index. Every index write, from the runner, the verify stage, batch enrichment
and rebuilds, holds the same lock from its read to its write, so no stage
writes back an index it read before another stage recorded a key.

- **No finished bundle has the key.** The bundle is written to the run's own
  directory as it always was, and the card lists the run as its first and
  only occurrence.
- **A finished bundle in another run's directory has the key.** The run is
  appended to that card's `occurrences`, and its own directory gets
  `bundle_ref.json` in place of a card, repair package and regression
  artifact. The pointer records the key and the `canonical_run_id` whose
  directory holds the card.

A finished bundle is a card carrying the key with the repair package and the
regression artifact beside it.

The card, the repair package and the regression artifact stay pinned to the
first occurrence. The card's text, evidence and blast radius describe that
run, and `occurrences` lists every run that repeated it along with the
provider, model and seed from each run's `run_config.json`.
`RunReader.get_bundle` and the dashboard serve a reproduction the card it
joined, whose `run_id` names the first occurrence.

By default the lookup covers one whole runs directory. Since the key names
the task, only runs of the same task share a card there, and the nineteen
failing task fixtures form nineteen keys.

### Scoping the lookup

`record_bundle`, `attribute_and_bundle` and `run_task_pipeline` take an
optional scope (`bundle_scope` on the pipeline), the run ids whose cards a
run may join. The run being bundled is always in its own scope, and a scoped
lookup reads those runs' cards directly without the index. None, the
default, searches the whole runs directory.

The branch stage is the caller the scope is for. Its conditions continue the
same fork with the control on, with it off and with another model, and
without a scope a failure in one condition joins a card from another. A
control-on run would then be served the card and regression artifact of a
control-off run. `branch` passes the runs of the condition it is running, so
each condition's cards stay apart, while its seeds still share one card per
key. A scope over the
whole experiment would merge the conditions again, so one condition's runs
is the scope to pass. The experiment's metrics count verdicts from batch
entries and come out the same either way. `run-sweep` scopes each cell to
the sweep's cells that have already completed with verdict fail, the cells
its retention keeps, so a failing cell never points to a card outside what is
retained with it, even in a runs directory that holds earlier runs or an
earlier sweep. Retention still refuses, naming them, any cell whose home lies
outside the retained cells ([live_sweep.md](live_sweep.md#retaining-failing-cells)).
A cell that ended incomplete after breaking a rule is bundled under the same
scope and can join a failing cell's card, but only failing cells are retained,
so the retained card lists only the runs retained with it.

Callers that copy runs somewhere else have to keep each reproduction with
the run holding its card. `ArtifactStore.bundle_home` names that run for one
run and `ArtifactStore.bundle_homes` for a set, so the homes a copy would
leave behind are the values outside the set. `RunReader.get_bundle_ref`
returns a reproduction's pointer, and `RunReader.get_occurrences` returns
the runs on the card covering any run. `RunReader.get_bundle` on a
reproduction whose home is missing from the runs directory raises
`FileNotFoundError` naming both runs.

### Bundling a run again

Bundling a run again never counts it twice. A reproduction already on the
card keeps its place, and the card's own run keeps its occurrences. A run
whose bundle moves elsewhere, because its key changed or a scope leaves out
its old card, leaves that card's occurrences. A run holding a card that
other runs point to refuses to become anything else with
`BundleKeyConflictError`, whether its key changed or another card with its
key won the lookup, because moving that card would leave their pointers
naming the wrong failure.

The card is written last, after the repair package and the regression
artifact, so a card marks a finished bundle. A bundle cut short therefore
leaves no card. `RunReader.get_bundle` reports the run as not bundled, the
next run with that key starts its own card, and bundling the interrupted run
again makes it a reproduction of that card and removes the files it had
written. A card found without the other two files, left by hand or by an
older writer, is passed over the same way.

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

A reproduction never touches the regression artifact of the card it joins,
so `replay`, the control library's evidence hashes and the collector's
`source_sha256` see the same bytes however many reproductions accrue. Once
written, a regression artifact changes only when its own run is bundled
again. Bundling a first occurrence again rewrites its three files from the
same run files, which gives the same bytes while the generator is unchanged.
A run that held its own bundle, written before 0.5.0 or under a key it no
longer has, deletes its regression artifact when bundled again into another
run's card, and anything that pins that file's hash loses the file.

`collect-regressions` discovers `regression_artifact.json` files, which only
first occurrences hold, so each key is replayed once per runs directory or
scope and reproductions are not counted in `artifacts_found`. Two retained
sweeps of the same failure keep two cards, and each is replayed. A bundle cut short before its card was
written can leave an artifact with no card beside it, which the collector
still finds. Bundling the run again either writes the card beside it or,
when the run joins another card, deletes it.
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
| `schema_version` | `str` | auto | hardcoded | Schema version; bump when fields are added or removed (`FailureCard 0.5.0`) |
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

| Prescribed `RepairControl.name` | Executable `guardrail_ref` | Seam | Static validation on the bundle suite |
|---|---|---|---|
| `deterministic_pre_call_refund_guardrail` | `unauthorized_cash_refund_guardrail` (`ctl_refund_window_v1`, the default, cash only) | pre-call | rejected, overblocks on the two cash refunds (`refund_policy_failure`, day 31 without approval); rejected, failure persists on the day 45 store credit, which is outside its scope |
| `deterministic_pre_call_refund_guardrail` | `unauthorized_refund_guardrail` (`ctl_refund_policy_v2`, cash and store credit) | pre-call | rejected, overblocks on all three |
| `current_policy_source_precedence` | `deprecated_policy_citation_guardrail` (`ctl_policy_source_v1`) | pre-call | rejected, overblocks |
| `ticket_claim_grounding_check` | `ticket_outage_claim_guardrail` (`ctl_ticket_grounding_v1`) | pre-call | accepted |
| `final_answer_state_grounding_check` | `final_answer_state_grounding_guardrail` (`ctl_final_answer_grounding_v1`) | final answer | skipped, incomplete; can never be accepted, see below |
| `required_escalation_enforcement` | `required_escalation_guardrail` (`ctl_required_escalation_v1`) | final answer | skipped, incomplete; can never be accepted, see below |
| `escalation_discipline_check` | none yet | | |
| `retrieval_before_action_check` | none yet | | |
| `expected_action_contract_check` | never; detection only, see below | | |
| `regression_test_ci_gate` | never; a CI-side control, #161 makes it real | | |

Every row with a guardrail is executable and blocks what its check names,
and each guardrail reads the rule the matching verifier check reads (#194).
The policy source guardrail applies its check's gate to the one call it
sees: a deprecated doc id in the arguments blocks the call only when current
policy would also forbid it, so a correct refund that notes "v2 is
deprecated, using v4" goes through.

The last column is what `replay --apply-control --control <id>` records
against the bundle suite's failing tasks. Only ticket grounding is accepted,
and the other verdicts have two different causes.

The refund and policy source verdicts come from static replay. When a refund
is blocked, the recorded script goes on to tell the customer the refund went
out, which adds `final_answer_inconsistent_with_state` and reads as
overblocking. A live agent sees the block and can answer differently, which
a script cannot do. That is the gap brief 001 measures and the reason
ADR-0002 treats static control verdicts as advisory.

The two final-answer controls can never be accepted, because a blocked final
answer ends the run. Whenever a hook on the final-answer seam from #193 blocks
an answer, the runner ends the run as `terminated` (`final_answer_blocked`),
with a scripted agent or a live one, and `decide_verdict` records a pinned
replay that did not complete as `skipped: validation_incomplete`. Every
validation in which one of these controls acts is therefore incomplete, and
neither control can be committed to the control library. Both act on the
bundle suite's static replays, because the replayed answer is the one the
check failed. A live run in which the control never fires could clear the
check, but that verdict would describe the agent, since the control did
nothing. The instruction in each block message ("Call escalate_case, then
answer.", "Describe what the tools actually did.") becomes the run's error
message and never reaches the agent.
Static replay is not the cause, so validating with a live agent does not fix
this. It needs a change at the #193 seam, such as handing the block back to
the agent as an observation so it can answer again, or a change to how
validation judges a blocked answer.

Only `ctl_refund_window_v1` is in the default set that `replay` installs and
the materializer uses to predict replay mode. The others are in
`control_catalogue()` and are selected with `--control`. Widening the default
set would change the replay label of every artifact and every pinned
expectation built on one, so that is left for a separate change. When both
refund controls are selected, each gets its own verdict under the one
prescription.

The ticket matcher is shared by the verifier and the guardrail, and
`fixtures/claim_matching/labeled_texts.json` holds 43 ticket texts both are
tested against. The matcher is a word list with a negation window, and
beyond that it applies narrow rules that each set aside one mention. An
existential question about the outage asks rather than claims. An
"incident" that names something other than the service is a support case. A
negation after the claim word counts only when it denies the outage
happened, and a hedge such as "if there was one" withdraws the claim. Each
rule has a case on either side of it in the set, and each rule's comment
names what it costs. None of them can add a claim, because a claim invented
on ticket text fails an agent that wrote a careful note. One text is pinned
as known wrong for that reason. In "there was no warning before the outage
hit" a negation about the warning suppresses a real claim, and a rule that
let the claim through would also fire on "no store credit because the
outage is not documented".

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

`replay --apply-control` writes `repair_validation.json` at `RepairValidation 0.3.0`
under `<output-runs-dir>/<source_run_id>/`, reading prescriptions beside the
input regression artifact. Invalid or mismatched packages fail before replay;
empty packages produce no verdicts. Without a package, selected reference
controls use all pinned checks and record `controls_source: reference_controls`.

Each verdict includes control identity, reason, originating and sibling run
IDs, failed checks, and linked checks cleared on completed replays. When two
selected controls materialize one prescription, each gets its own verdict
under the prescription's name, told apart by `control_id`. Evidence
retains `PASS`, `FAIL`, or `INCOMPLETE`; a rollup counts the control verdicts.

ADR-0002, decision 2: "A static replay verdict on a control is advisory
until the artifact carries a measured replay-mode label." The same decision
has the collector gate control results only on `static_ok`. Every
`static_ok` label today is predicted by the materializer's fixed rule
(`replay_mode_basis.predicted_by`), and #159 is what measures one.
Validation follows the collector, so it calls a verdict on a `static_ok`
artifact whose own basis supports the label gating, and every place that
prints gating also says the label is predicted.

Each verdict records the artifact's `replay_mode`, its `predicted_by`,
`label_supported` (whether the artifact's recorded basis classifies as its
label), and the `standing` those give it: `gating` for a `static_ok` label
with a recorded basis that classifies as `static_ok`, `advisory` otherwise.
The verdict values are unchanged, so an advisory `accepted` still means the
control held under replay, and it makes no claim about a live agent. The
rollup splits `accepted` into `accepted_gating` and `accepted_advisory`.
`standing` is derived on read from the recorded fields, so editing
`standing` alone changes nothing. Editing the recorded fields would change
it, which is why the control library and the metrics check a verdict's
label against the retained artifact, and its basis against the
classification rule, before treating it as gating. When an artifact's
`static_ok` label is not supported by its own basis, as with a label set by
hand, replay prints a warning and records the verdicts as advisory. A
`0.1.0` file, written before #228, carries no label. Its verdicts read as not recorded and advisory, and an unrecorded
label is never compared with the artifact's current one.

Each re-run also records the `task_fixture` it was built from, and
`rollup.over_blocking` reports sibling failures by task family with a
one-sided 95% upper bound on the family failure rate. The formula and why
it counts families are in `docs/methodology_metrics.md` (A4). Replay and
`inspect` print it as, for example, `0 of 1 families failed, true rate
could be up to 95.00%`.

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

Each entry records the basis of its acceptance in `acceptance`: the
`replay_mode` and `predicted_by` of the originating artifact at commit time,
and the `standing` they support. The rule:

- A control accepted against a `static_ok` artifact whose recorded basis
  still classifies as `static_ok` enters as `gating`. That label is a
  prediction until #159 measures it, and `controls list` says so.
- Any other accepted control still enters, and is recorded as `advisory`.
  That covers `live_required` and `unlabeled` artifacts and a `static_ok`
  label with no basis or with a basis that does not classify as `static_ok`.
- Advisory entries install like any active entry, so suites and replays run
  with them in place and measure their effect. Nothing downstream may report
  an advisory entry as proven.
- Loading holds a recorded basis to the retained artifact and validation. A
  basis naming a different `replay_mode` or `predicted_by`, a `gating` basis
  the artifact does not support, an `advisory` basis on an artifact that
  does support gating, and a basis that names a predictor without a
  `replay_mode` all fail to load. A validation verdict is compared with the
  artifact only when it recorded a `replay_mode`, and then its
  `predicted_by` and `label_supported` must match the artifact too. A
  verdict with no `replay_mode` cannot record a predictor or a supported
  label.

Library schema `0.2.0` adds the field. An entry without it, which covers
every entry written before this schema including `ctl_refund_window_v1`,
reads as `advisory` with its `replay_mode` not recorded. That holds
whatever its retained artifact says: `ctl_refund_window_v1`'s artifact is
`unlabeled`, and a library built by earlier code from a current artifact
has a `live_required` one. Nothing at acceptance time recorded which label
the verdict relied on, so the entry is not compared with the artifact, and
a basis with no `replay_mode` cannot be `gating`. The first write of an
older library records that basis explicitly. `trace-harness controls list`
prints every entry with its status and basis, and `run-suite
--control-library` prints the gating and advisory split of what it
installed.

The [retained example](../fixtures/controls/README.md) preserves all 18
passing suite cases. Two negatives lose `unauthorized_cash_refund` but retain
false refund claims, so they still fail. Separate controlled expectations
record those outcomes. The full CI collector remains #161; static replay
remains advisory for live-agent recovery under ADR-0002.

### `RepairPackage` fields

| Field | Type | Required | Description |
|---|---|---|---|
| `schema_version` | `str` | auto | `RepairPackage 0.3.0` |
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
