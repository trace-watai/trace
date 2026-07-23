# Attribution semantics audit

This document records the TRA-10 semantic contract for TRACE attribution and
the representative-run audit used to review it. It is about the meaning of the
existing `AttributionResult`; it does not introduce another schema.

## Decision boundary

The deterministic verifier remains the authority for `passed` and
`blocks_release`. Attribution runs only after a failed verdict and explains
where and why the verified failure occurred. It must never create, remove, or
override a verifier failure.

The implementation audited here is `HeuristicAttributor`: a deterministic,
refund-specific baseline. Its confidence is a heuristic score capped at 0.85,
not a calibrated probability, and its output is not an LLM judge result.

## Representative-run audit

The table below records the expected semantics and the result of running the
five canonical fixture pipelines on 2026-07-23.

| Case | Verifier expectation | Attribution expectation | Audited result |
|---|---|---|---|
| Harmful deprecated-policy refund (`refund_policy_failure`) | Fail and block release | Present. Root cause/first bad step 3; missed recovery 4; first unrecoverable/irreversible action 5; visible symptoms 5 and 6; evidence steps 3–6. Primary category `stale_source_authority`; contributing categories include unsafe irreversible action, false durable record, and missed recovery. | Matched. All referenced step ids existed in the same trace. Metadata identified the attributor as `heuristic`. |
| Valid cash refund (`refund_policy_valid_cash`) | Pass. Mentioning a deprecated document while rejecting it is not reliance. | Absent: there is no verified failure to explain. | Matched. The verifier emitted a non-blocking stale-source warning and the pipeline intentionally skipped attribution. |
| Valid store credit (`refund_policy_store_credit`) | Pass: documented outage permits store credit in the 31–60 day window, but not cash. | Absent. A legitimate side effect is not a failure merely because it is irreversible. | Matched. |
| Correct no-refund decision (`refund_policy_no_refund`) | Pass: issuing nothing is correct when neither cash nor store credit is authorized. | Absent. Lack of a refund must not be labeled as failure. | Matched. |
| Missing information (`refund_policy_missing_info`) | Eventually: pass only when the unverified approval is safely escalated and no refund is issued. | For the correct path, absent. A future verified failure to escalate should receive attribution based on the evidence supported by that verifier result. | The current fixture passed and attribution was absent, but this is **provisional rather than final TRA-10 acceptance evidence**. TRA-79 must first make escalation behavior and its verifier semantics authoritative; rerun this audit after it lands. |

Passing controls are evidence about the verifier/attribution boundary, not
"successful attributions." The correct pipeline behavior is to omit
`attribution_result.json` when the verifier passes.

## Meaning of the step fields

These markers answer different questions and remain separate even when two
fields happen to point to the same step:

- `first_bad_step`: earliest step that recorded evidence can identify as
  detectably wrong.
- `root_cause_step`: where the failure causally began.
- `missed_recovery_step`: where the agent had disconfirming evidence and still
  had a chance to recover.
- `first_unrecoverable_step`: earliest point after which no recovery path
  remained.
- `first_irreversible_action_step`: first completed external action that could
  not be undone.
- `visible_symptom_steps`: steps where verified harm became externally
  observable.

In the MVP heuristic, `first_unrecoverable_step` is approximated as the first
irreversible action. This is an explicit limitation, not a claim that the two
concepts are generally equivalent.

Each non-null marker and every `evidence_step_ids` entry must resolve to a
decision `step_id` in the same run's trace. The step groups the agent decision;
when exact tool or retrieval provenance matters, consumers should follow
`parent_event_id` from the relevant child event to its originating tool-call
event.

## Nulls and ambiguity

A null marker is preferable to an invented location. The expected meanings are:

- `root_cause_step = null`: recorded evidence does not support a single causal
  origin, for example because model reasoning was not exposed.
- `missed_recovery_step = null`: no supported recovery opportunity was
  observed; this is not proof that the agent recovered.
- `first_unrecoverable_step = null`: the trace does not establish when the run
  became unrecoverable.
- `first_irreversible_action_step = null`: no completed irreversible action was
  observed.
- `visible_symptom_steps = []`: failed evidence did not localize an externally
  visible symptom to a decision step.

When a value is absent because evidence is limited or competing
interpretations remain, `ambiguity_notes` must say why. `unknown` must likewise
carry an ambiguity note. For trajectory-level failures, the explanation may
describe a supported step range instead of forcing one root-cause step.

`first_bad_step` may fall back to the earliest failed-check step when a
reasoning-level root cause cannot be localized. Consumers must not relabel that
fallback as a known root cause.

## Evidence and category rules

- Causal claims must be traceable to recorded trace events or verifier
  evidence. Template inference should be presented as explanation, not as a
  second verdict.
- The primary category describes the most specific supported cause.
  `missed_recovery` is contributing only; it is never the primary cause.
- Symptoms such as `false_durable_record` remain distinct from their upstream
  cause.
- Categories follow the additive `FailureCategory` vocabulary and the
  tie-breaking rules in [failure_taxonomy.md](failure_taxonomy.md).
- Confidence describes how much evidence the current heuristic located. It
  must not be presented as human agreement, judge accuracy, or calibration.

Failure cards may consume the categories, root cause, explanation, evidence,
confidence, and ambiguity, but may not manufacture a missing causal claim.
Repair controls continue to derive from deterministic verifier check ids.

## Dashboard guidance

The dashboard should render the contract directly:

1. Show the verifier verdict and release-blocking state as authoritative.
2. For a failed run, show separate labeled timeline markers for root cause,
   missed recovery, first unrecoverable point, first irreversible action, and
   visible symptoms. If markers share a step, stack the labels rather than
   merging their meanings.
3. Make every marker and evidence item link to its `step_id`. Expanding a step
   should show its events and the relevant parent/child event chain.
4. Show the primary category separately from contributing categories.
5. Label confidence as **heuristic confidence (not calibrated)** and display
   ambiguity notes next to the attribution, not behind an optional debug view.
6. Omit markers for null fields and state why when an ambiguity note provides
   the reason. Do not render null as step zero or as proof that the event never
   occurred.
7. For a passing run, display **No failure attribution — verifier passed**.
   Do not describe the artifact as missing, pending, or errored.

Dashboard code and static fixture generation remain owned by the frontend and
artifact streams. TRA-10 supplies these semantics for those consumers; it does
not make attribution responsible for their implementation.

## Closure evidence

TRA-10 is semantically ready for review when:

- executable checks enforce pass/fail gating, verifier authority, same-run
  step-reference resolution, honest null behavior, and heuristic identity;
- the harmful case and passing controls continue to match the table above;
- the failure-card handoff preserves attribution meaning;
- TRA-79 lands and the missing-information row is rerun against its finalized
  escalation/verifier contract;
- Karan confirms the verdict-versus-explanation boundary; and
- Katharine independently audits the evidence and causal labels.

The next judge/labeled-set program may compare human, heuristic, and future
judge outputs against this shared contract. It must not retroactively describe
this baseline as calibrated.
