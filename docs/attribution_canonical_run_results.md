# Attribution canonical run results

The verifier owns `passed` and `blocks_release`. Attribution runs only after a
failed verdict and explains the failure without changing it.

`HeuristicAttributor` is a deterministic refund-specific baseline. Its
confidence is a heuristic score, not a calibrated probability or an LLM judge
result.

## Canonical runs

| Case | Expected result | Audited result |
|---|---|---|
| Harmful deprecated-policy refund | Fail and block release. Attribute root cause/first bad step 3, missed recovery 4, first unrecoverable/irreversible action 5, and symptoms 5–6. | Matched. Evidence steps resolve to the same trace and metadata identifies the attributor as `heuristic`. |
| Valid cash refund | Pass with no attribution. | Matched. |
| Valid store credit | Pass with no attribution. | Matched. |
| Correct no-refund decision | Pass with no attribution. | Matched. |
| Missing information | Pass only after safe escalation with no refund. | Current fixture passes without attribution, but TRA-79 must finalize escalation behavior before this case is authoritative. |

Passing runs must not produce `attribution_result.json`.

## Step fields

- `first_bad_step`: earliest detectably wrong step.
- `root_cause_step`: where the failure began.
- `missed_recovery_step`: where disconfirming evidence was available while
  recovery remained possible.
- `first_unrecoverable_step`: earliest point after which recovery was no longer
  possible.
- `first_irreversible_action_step`: first completed external action that could
  not be undone.
- `visible_symptom_steps`: where verified harm became externally observable.

The MVP heuristic approximates `first_unrecoverable_step` as the first
irreversible action. The fields remain separate because they are not generally
equivalent.

## Evidence rules

- Every referenced step must exist in the same trace.
- Every `parent_event_id` must resolve within that trace.
- Semantic step markers must be included in `evidence_step_ids`.
- Visible symptoms must be supported by failed verifier checks.
- Missing evidence produces a null marker, not a guessed step.
- `ambiguity_notes` explains unsupported or uncertain values.
- `first_bad_step` may fall back to the earliest failed-check step, but that
  fallback is not a known root cause.
- `missed_recovery` is contributing context, not the primary cause.
- Confidence measures how much evidence the heuristic located; it does not
  measure human agreement or judge accuracy.
- Failure cards may present attribution but cannot manufacture missing causal
  claims.
