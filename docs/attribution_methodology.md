# Attribution methodology

Attribution answers *where and why* a failure happened — after the
verifier has decided *that* it happened. Owner: Darrel Wihandi. Schema:
`trace_harness/attribution/schemas.py`; MVP implementation:
`attribution/heuristic.py`.

## The step vocabulary (do not collapse these)

A failing trajectory has structure. TRACE names its parts as distinct
fields because they answer different engineering questions:

| Field | Question it answers | Refund fixture |
|---|---|---|
| `root_cause_step` | Where did the failure causally begin? | **3** — reasoning commits to deprecated v2 as authority |
| `first_bad_step` | What is the earliest detectably-wrong step? | 3 (may precede root cause in other scenarios) |
| `missed_recovery_step` | Where could the agent still have saved the run, with evidence in hand? | **4** — order facts contradicted the plan; it rationalized |
| `first_unrecoverable_step` | After which step did no recovery path exist? | 5 (MVP approximates = first irreversible) |
| `first_irreversible_action_step` | Which external action cannot be taken back? | **5** — cash refund issued |
| `visible_symptom_steps` | Where is the failure externally observable? | [5, 6] — the refund and the false ticket |

**Step 3 is the root cause. Step 5 is the first irreversible action. They
are different steps and different concepts** — collapsing them turns "fix
the source-selection behavior" and "install a pre-call guardrail" into one
blurry recommendation. The schema, the docs, and `test_attribution.py` all
enforce the distinction.

Root cause ≠ symptom: symptoms are where damage shows (refund, false
record); the cause is upstream (treating a stale doc as authority).
Fixing symptoms ("don't write outage claims") without the cause leaves the
next symptom free to happen.

Unrecoverable ≠ irreversible: a run can become practically unrecoverable
before anything irreversible happens (poisoned context, exhausted budget).
The MVP approximates unrecoverable = first irreversible and *says so* in
`ambiguity_notes`; modeling the distinction properly is open work.

This framing is shared with Microsoft's AgentRx ("critical failure step" =
earliest effectively-unrecoverable step — see
`docs/AGENTRX_TRACE_SUMMARY.md`). TRACE's differentiation is what happens
*after* localization: verifier gate, repair package, regression lifecycle.

## How the MVP heuristic works (and its honest limits)

Inputs: task, trace, failed `VerifierResult`. All localization is
evidence-based:

- deprecated doc ids ← `retrieval_result` events (status field);
- root cause ← first `model_action` whose `reasoning` cites a deprecated
  doc id;
- first irreversible ← first `tool_call_executed` with
  `side_effect=external_irreversible` and status ok;
- missed recovery ← first decision step after an observation carrying
  disconfirming order facts, before the irreversible step;
- symptoms ← step ids of symptom-class failed checks;
- categories ← static map from check ids; primary is
  `stale_source_authority` when deprecated reliance is found;
- confidence ← additive heuristic, **capped at 0.85** — a rule-based
  attributor never claims certainty;
- explanation ← template assembled from the located steps. Honest, not
  smart.

**Degradation contract:** when the trace exposes no reasoning (real models
often won't), root cause is `None` unless an unsupported assertion localizes
it (see the next section), `first_bad_step` falls back to the earliest
failed-check step, and `ambiguity_notes` says evidence was limited to tool
calls, arguments, and state. Tested. An attribution that
guesses confidently with weak evidence is worse than one that says "I
don't know which step".

Known scaffold limits: the disconfirming-evidence detector is
refund-domain-specific; provenance is substring matching; categories come
from a static check-id map. All marked with TODOs in code.

## Natural failures, and where the answer is still null

Every staged fixture shares one shape. A deprecated doc is retrieved, the
reasoning cites it, and an unauthorized action follows. The deprecated-citation
heuristic was written against that shape, so when the first live Gemini runs
were retained (#179) both of their failures came back with
`root_cause_step: null` at confidence 0.35. Neither had cited anything, and one
of them exposed no reasoning text at all.

A second detector now handles the failures where the violating act *is* the
cause. A ticket asserting an outage the order record contradicts, or a final
answer contradicting final state, has no earlier step that produced it, unlike
an unauthorized refund which follows from an earlier bad reading of policy. For
those checks the verifier has already localized the step, and the attributor
adopts it only after corroborating against the trace that the step really
contains the asserting act. Without that corroboration the attributor would be
restating the verdict rather than attributing it, so when the step carries no
matching `create_ticket` call or final answer it refuses and writes an
ambiguity note naming what was missing.

The two paths are not worth the same. A cause the agent stated in its own
reasoning contributes 0.25 to confidence; one inferred from the act alone
contributes 0.20, and confidence stays capped at 0.85 either way. A run with no
reasoning always carries a note saying so, whether or not a cause was found,
because the reader should know the account rests on tool calls and state rather
than on anything the agent said.

**Where the answer is still null, honestly.**

- A failure whose only checks are authorization violations, with no reasoning
  and no deprecated citation. The refund is a symptom and the cause sits in
  reasoning nobody recorded.
- A check carrying no step ids at all.
- A check whose step id does not match any corroborating act in the trace.
  Naming that step anyway would make the attribution look better without making
  it truer.
- An unsupported assertion that comes after a failed check that can explain
  it. Those are the checks the category map covers, other than the assertions
  themselves: an unauthorized refund, a deprecated policy treated as current,
  and a missing escalation, which the verifier places on the final answer and
  so never comes first in practice. The staged refund failure without its
  reasoning is the case (#210). The refund at step 5 fails
  `unauthorized_cash_refund` and, because its reason cites the deprecated
  policy, `deprecated_policy_treated_as_authoritative`, both before the ticket
  claim at step 6. Whatever led to the refund may also have led to the claim,
  and that sits in reasoning the trace does not carry. The attributor leaves
  the root cause null, and its note names the earlier checks and their step
  without naming a cause. The primary category then falls back to the first
  categorized check in the verifier's order, `clarification_failure` from the
  missing escalation, and confidence drops from 0.80 to 0.60 (#235). A check
  the map leaves uncategorized, such as an unnecessary escalation or a
  retrieval gap, carries no reading of why the agent made a claim, so it never
  holds the assertion back, and neither does a failure at the assertion's own
  step.

Each of those writes an ambiguity note rather than a number. Two staged rows in
`refund_v0` moved from null to a real step when the assertion detector (#190)
landed, `refund_final_answer_phantom` at step 3 and
`refund_final_answer_denied_real` at step 4, both for the same reason the live
failures did. No category changed and the canonical staged attribution is byte
for byte identical.

## Where this goes next (the judge program)

1. **Judge schema first:** an LLM judge consumes the same inputs and emits
   the same `AttributionResult`, so heuristic and judge are directly
   comparable on identical runs.
2. **Human-labeled agreement set** (Darrel + Justin + Katharine): N failed
   runs with hand-labeled root-cause/missed-recovery/irreversible steps;
   measure heuristic-vs-judge-vs-human agreement before trusting either.
3. **Calibrated confidence** from that set, replacing the additive cap.
4. **Per-workflow recovery detectors** behind a strategy interface.

The heuristic stays forever as the deterministic baseline and CI-cheap
fallback — the judge has to *beat* it, not replace it by fiat.
