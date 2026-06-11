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
often won't), root cause is `None`, `first_bad_step` falls back to the
earliest failed-check step, and `ambiguity_notes` says evidence was
limited to tool calls, arguments, and state. Tested. An attribution that
guesses confidently with weak evidence is worse than one that says "I
don't know which step".

Known scaffold limits: the disconfirming-evidence detector is
refund-domain-specific; provenance is substring matching; categories come
from a static check-id map. All marked with TODOs in code.

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
