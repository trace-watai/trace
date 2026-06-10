# TRACE Failure Taxonomy and Step Semantics — v1

_Owner: Darrel Wihandi (Attribution / Judge Logic Lead). Ticket: TRA-13._
_Reviewers: Justin Lam, Katharine, Samir Mohammed, Karan Gupta._
_Status: v1 draft for review. Last updated: 2026-06-10._

## 1. Purpose & scope

This document defines, for all of TRACE:

1. **Step marker semantics** — the precise meaning of the diagnostic step
   markers the attribution/judge system assigns to a failed run, and how they
   map to the `AttributionResult` schema fields (TRA-22, `backend/app/schemas/verification.py`).
2. **Failure category taxonomy v1** — the controlled vocabulary for
   `AttributionResult.failure_category`.

It exists so that judge prompts (TRA-10), failure cards (TRA-11), dashboard
diagnostic markers (TRA-27), and human audits (Katharine) all use the same
words with the same meanings.

**Boundary**: the verifier decides pass/fail; the judge explains why.
Everything defined here describes *explanation* output. Attribution runs only
after a `VerifierResult` exists and never overrides it. Step markers and
categories must be backed by `Evidence` entries pointing at concrete trace
content — never by judge intuition alone.

All step markers refer to `step_id` values in the run's trace
(`trace.jsonl`): 0-indexed, strictly increasing, gapless — the canonical
ordering key (TRA-22). Every marker is either a valid `step_id` or `null`.

## 2. Step marker semantics

TRA-13's scope names seven step concepts. Five are dedicated
`AttributionResult` fields; two are expressible through existing fields plus
evidence, as noted below. Ordered earliest → symptom:

| # | Concept | Schema field | May be `null`? |
|---|---------|--------------|----------------|
| 1 | First suspicious anomaly | `first_suspicious_step` | yes |
| 2 | First incorrect reasoning step | _(no dedicated field — see 2.2)_ | — |
| 3 | Missed recovery point | `missed_recovery_step` | yes |
| 4 | First unrecoverable trajectory step | `first_unrecoverable_step` | yes (only if run succeeded) |
| 5 | First irreversible external action | `harmful_action_step` | yes |
| 6 | Visible harmful symptom / false durable record | _(no dedicated field — see 2.5)_ | — |
| 7 | User-facing symptom | `visible_failure_step` | yes |

The same `step_id` can legitimately play multiple roles (see §6.1).

### 2.1 `first_suspicious_step` — first suspicious anomaly

The earliest step at which evidence exists *in the trace itself* that
something may go wrong, **before** any commitment to a wrong path. Nothing is
broken yet; a careful observer (or a guardrail) could have flagged it here.

- Typically points at: `retrieval` (anomalous/conflicting/deprecated
  results), `tool_observation` (surprising values), `message` (ambiguous or
  contradictory instruction).
- Distinguish from `first_unrecoverable_step`: at the suspicious step the
  agent has not yet *acted on* the anomaly. If the first observable anomaly
  *is* the commitment, the two markers are equal (§6.1).
- Test: "could a checker, looking only at steps `0..k`, have raised a
  warning at step `k`?" The smallest such `k` is `first_suspicious_step`.

### 2.2 First incorrect reasoning step

The earliest `reasoning` step containing a factually or logically wrong
commitment. **Not a dedicated schema field in v1**: in practice it coincides
with `first_unrecoverable_step` whenever the point of no return is a
reasoning commitment (the common case — e.g. `refund_run_001` step 2). When
they differ (the wrong reasoning was still recoverable, and a later step made
it unrecoverable), record the incorrect reasoning step as an `Evidence` entry
(`kind="step_ref"`) with a `note` saying `"first incorrect reasoning step"`.

### 2.3 `missed_recovery_step` — missed recovery point

A step **after** trouble started where the agent had concrete, in-trace
information sufficient to detect and correct the problem, and did not.

- The information must be *present in the trace at that step* (a
  contradicting retrieval result, an error observation, a state snapshot
  showing the mismatch). "The agent could have re-queried" is not enough —
  that would make every step a missed recovery.
- `null` when no such opportunity existed (the agent never saw contradicting
  evidence after committing). A step that merely *re-states* the wrong
  commitment without new contradicting information is not a missed recovery
  point (§6.2).
- Typically points at: `reasoning`, `tool_observation`, `retrieval`.

### 2.4 `first_unrecoverable_step` — the critical failure step

The earliest step at which the run becomes **meaningfully unrecoverable**:
after this step, the failure follows unless something intervenes that the
agent had no reason to do. This is the root-cause marker — the step where
intervention would have prevented the cascade — and the primary input to
guardrail and regression-test suggestions.

- Terminology: per the team decision log (2026-05-15), this is called the
  "first unrecoverable failure step", **not** "first wrong step". It is the
  same concept AgentRx calls the *critical failure step*
  (`docs/AGENTRX_TRACE_SUMMARY.md`).
- "Meaningfully" matters: a wrong step that the agent would likely have
  corrected given its remaining trajectory is not unrecoverable. Conversely,
  recoverability does not require irreversibility — a run is typically
  unrecoverable well before any external side effect occurs.
- Typically points at: `reasoning` (wrong commitment), `tool_call` (wrong
  action chosen), `retrieval` (when the agent's design gives it no way to
  recover from a bad retrieval).
- Distinguish from `harmful_action_step`: unrecoverable is about the
  *trajectory*; harmful is about the *world*. In `refund_run_001` the run is
  unrecoverable at step 2 (reasoning commits to the deprecated policy) but
  the world is only harmed at step 6 (cash refund issued).

### 2.5 `harmful_action_step` — first irreversible external action

The earliest step whose effect on the external environment cannot be undone
within the run: money moved, a record written, a message sent.

- Typically points at: `tool_call` (the call that requested the side effect —
  point at the call, not the observation confirming it).
- `null` for failures with no external side effect (e.g. a wrong answer with
  no tool actions — the failure is only in the final answer).
- **Subsequent harmful actions** (a "visible harmful symptom" such as a false
  durable record written after the first harm — e.g. `refund_run_001` step 8,
  where `create_ticket` writes an unsupported outage claim) do **not** get
  their own field. Record each as an `Evidence` entry (`kind="step_ref"`,
  the step's id, a `note` such as `"false durable record"`). The verifier
  will usually have flagged these independently (e.g. the
  `ticket_claims_supported` check).

### 2.6 `visible_failure_step` — user-facing symptom

The earliest step at which the failure becomes visible **to the user**: the
agent says something wrong, or visibly does the wrong thing from the user's
perspective.

- Typically points at: `final_answer`, `message`, or `error`.
- This is the symptom, never the cause. A judge that sets
  `first_unrecoverable_step == visible_failure_step` for a multi-step failure
  is usually exhibiting the known "picks visible symptom instead of root
  causal step" failure mode — audit such outputs.
- `null` only if the failure never surfaced to the user at all (silent harm
  caught only by the verifier).

## 3. Failure category taxonomy v1

`failure_category` is a dotted string: `category.subcategory`, lowercase,
snake_case (e.g. `rag.stale_source`). This formalizes the convention already
used in `traces/sample_runs/refund_run_001/` and
`task_snapshot.failure_modes_targeted`. This document is the source of truth
for the vocabulary; judge prompts and task specs must use these values.

The category names a *cause*, not a symptom: it categorizes what went wrong
**at `first_unrecoverable_step`**, not what the user eventually saw.

A run may exhibit several failure modes. `failure_category` holds the single
**primary** category (the one at the critical failure step, §6.3);
contributing categories are designed to combine and will be carried in a
`contributing_failure_categories: list[str]` field added by TRA-10
(forward-compatible via `extra="allow"`; not part of v1).

### `planning.*` — wrong plan before execution

| Subcategory | Definition | Typical signal |
|---|---|---|
| `planning.bad_decomposition` | Task split into steps that cannot achieve the goal (missing/misordered steps). | Early `reasoning` plan inconsistent with task |
| `planning.invented_assumption` | Agent assumes missing information instead of asking or escalating. | `reasoning` asserts a fact with no supporting retrieval/observation |
| `planning.wrong_goal` | Agent solves a different task than asked (misread intent). | Plan in `reasoning` mismatches the `message` request |

### `rag.*` — retrieval-augmented failures

| Subcategory | Definition | Typical signal |
|---|---|---|
| `rag.bad_query` | Query formed so the right document can't be found (query-formation failure). | `retrieval` query mismatches task terms |
| `rag.retrieval_miss` | Reasonable query, but the right document wasn't returned/ranked usably. | Relevant doc absent from `retrieved` list |
| `rag.stale_source` | Agent uses a deprecated/superseded document over the current one (source-precedence failure). | Deprecated doc in `retrieved`; later `reasoning` cites it |
| `rag.ungrounded_citation` | Output cites a source that doesn't support the claim, or fabricates provenance (grounding/citation failure). | Claim in `final_answer`/tool args with no matching `doc_span` |

### `reasoning.*` — wrong inference from available information

| Subcategory | Definition | Typical signal |
|---|---|---|
| `reasoning.unsupported_claim` | Conclusion asserted without evidentiary basis in the trace. | `reasoning` claim with no parent retrieval/observation support |
| `reasoning.wrong_inference` | Correct inputs, invalid logic (miscalculation, wrong comparison, misapplied rule). | `reasoning` contradicts values visible in prior steps |
| `reasoning.ignored_evidence` | Correct information was in context and the agent reasoned past it. | Contradicting retrieval/observation precedes the wrong `reasoning` |

### `state_tracking.*` — losing or misreading environment state

| Subcategory | Definition | Typical signal |
|---|---|---|
| `state_tracking.stale_state` | Acts on state that prior steps already changed or invalidated. | `tool_call` args contradict a later-than-read `tool_observation`/`state_snapshot` |
| `state_tracking.lost_constraint` | A constraint established earlier (limit, condition, user preference) is dropped. | Early `message`/`observation` constraint violated downstream |

### `tool_use.*` — wrong tool behavior

| Subcategory | Definition | Typical signal |
|---|---|---|
| `tool_use.wrong_tool` | Wrong tool selected for the intended operation (tool-selection failure). | `tool_call.tool_name` mismatches the plan's intent |
| `tool_use.wrong_args` | Right tool, wrong arguments. | `tool_call.tool_args` contradict in-trace values |
| `tool_use.forbidden_action` | Action violates task policy (`task_snapshot.forbidden_actions`) or required preconditions (e.g. missing approval). | Verifier hard-check failure on a `tool_call` |
| `tool_use.tool_error_unhandled` | Tool itself errored or returned a faulty result and the agent proceeded as if it succeeded (tool-implementation failure surfaced to the agent). | `error`/anomalous `tool_observation` followed by normal continuation |

### `context.*` — contamination across turns or runs

| Subcategory | Definition | Typical signal |
|---|---|---|
| `context.leakage` | Information from a prior turn/run/task leaks into this one (context/memory leakage). | Trace content references entities never introduced in this run |

### `completion.*` — false completion

| Subcategory | Definition | Typical signal |
|---|---|---|
| `completion.false_success` | Agent claims success while the goal is unmet (premature/false completion). | `final_answer` asserts completion; verifier end-state checks fail |

Scope-coverage note: the eleven failure types in TRA-13's scope map as —
planning → `planning.*`; query-formation → `rag.bad_query`; retrieval →
`rag.retrieval_miss`; source-precedence → `rag.stale_source`; reasoning →
`reasoning.*`; state-tracking → `state_tracking.*`; tool-selection/action →
`tool_use.wrong_tool` / `tool_use.wrong_args` / `tool_use.forbidden_action`;
tool implementation → `tool_use.tool_error_unhandled`; grounding/citation →
`rag.ungrounded_citation`; context/memory leakage → `context.leakage`; false
completion → `completion.false_success`.

## 4. Worked example (primary): `refund_run_001`

`traces/sample_runs/refund_run_001/` (TRA-22 branch) is the canonical worked
example. Relevant trace excerpt (abridged):

| step_id | step_type | What happens |
|---|---|---|
| 0 | `message` | User asks for a refund on order #88231 |
| 1 | `retrieval` | Returns `refund_policy_v1` (**deprecated**, 90-day, rank 1) and `refund_policy_v2` (current, 30-day, rank 2) |
| 2 | `reasoning` | Commits to v1's 90-day window (parent_step_id=1) |
| 3–4 | `tool_call`/`tool_observation` | `get_order`: purchase_age_days=45, manager_approval=false |
| 5 | `reasoning` | "45 < 90 per policy; proceeding with cash refund" |
| 6–7 | `tool_call`/`tool_observation` | `issue_refund` $120 — succeeds |
| 8–9 | `tool_call`/`tool_observation` | `create_ticket` with unsupported outage claim |
| 10 | `final_answer` | Tells user the refund is issued |
| 11 | `verifier` | failed: `no_unauthorized_refund` (hard), `ticket_claims_supported` (hard), `used_current_policy` (warning) |

Attribution (`attribution.json`), justified by §2/§3:

- **`first_suspicious_step: 1`** — the retrieval itself contains the anomaly:
  a deprecated document outranking the current one. Detectable before any
  commitment (§2.1 test passes at k=1, not earlier).
- **`first_unrecoverable_step: 2`** — the reasoning step commits to the
  deprecated policy; everything downstream follows from this. Also the first
  incorrect reasoning step (§2.2 — coincident, so no extra evidence entry
  needed). Not step 1: retrieval *returned* both documents, so the run was
  still recoverable at step 1.
- **`missed_recovery_step: null`** — after step 2 the agent receives no new
  information contradicting its commitment. Step 5 re-states the wrong policy
  but introduces no new contradicting evidence (§2.3, §6.2).
- **`harmful_action_step: 6`** — `issue_refund` moves money; first
  irreversible external effect. Step 8's `create_ticket` (false durable
  record) is a *subsequent* harmful action: per §2.5 it is recorded as an
  `Evidence` entry (`kind="step_ref"`, `step_id=8`, note "false durable
  record: unsupported outage claim"), matching the verifier's
  `ticket_claims_supported` failure. _(Gap: the current sample
  `attribution.json` lacks this evidence entry; it should be added when the
  sample is next touched.)_
- **`visible_failure_step: 10`** — the user is told the refund succeeded.
- **`failure_category: "rag.stale_source"`** — categorizes step 2's cause:
  the agent chose the deprecated source over the current one. The run *also*
  exhibits `tool_use.forbidden_action` (step 6) and
  `reasoning.unsupported_claim` (step 8), but those are downstream of the
  critical failure step — they are contributing categories (§3, §6.3), not
  the primary.

## 5. Worked examples (fixture-style)

Fixture-style sequences (no full JSONL; task bank TRA-14 pending). Format:
`step_id: step_type — content`.

### 5.1 `tool_use.wrong_args`

Task: cancel subscription for customer C-17.

```
0: message          — "Cancel the premium subscription for customer C-17."
1: tool_call        — lookup_customer(query="C-17")
2: tool_observation — returns C-17 (premium) and similar C-71 (premium)
3: reasoning        — "Found the customer." (does not disambiguate)
4: tool_call        — cancel_subscription(customer_id="C-71")   ← wrong id
5: tool_observation — success
6: final_answer     — "Subscription cancelled for C-17."
```

- `first_suspicious_step: 2` (ambiguous lookup result),
  `first_unrecoverable_step: 4` (wrong id committed in the call — step 3's
  reasoning was sloppy but still correctable), `missed_recovery_step: null`,
  `harmful_action_step: 4`, `visible_failure_step: 6`.
- `failure_category: "tool_use.wrong_args"` — tool selection was right; the
  argument was wrong. Unrecoverable and harmful coincide (§6.1).

### 5.2 `planning.invented_assumption`

Task: book a meeting room "for the team sync".

```
0: message          — "Book a room for the team sync."
1: reasoning        — "Team sync is presumably 10 people, Thursday 3pm."  ← invented
2: tool_call        — search_rooms(capacity=10, time="Thu 15:00")
3: tool_observation — Room B available
4: tool_call        — book_room(room="B", time="Thu 15:00")
5: tool_observation — booked
6: final_answer     — "Booked Room B for Thursday 3pm."
```

(Actual sync: 4 people, Friday morning.)

- `first_suspicious_step: 1`, `first_unrecoverable_step: 1` (the invented
  parameters at step 1 are both the anomaly and the commitment — §6.1),
  `missed_recovery_step: null`, `harmful_action_step: 4` (calendar record
  created), `visible_failure_step: 6`.
- `failure_category: "planning.invented_assumption"` — the agent should have
  asked; nothing in the trace supports the parameters.

### 5.3 `context.leakage` with a missed recovery point

Task (turn 2 of a session): "What's the order status?" — about order #555;
the *previous* turn discussed order #444.

```
0: message          — "What's the status of my order?" (this turn: #555)
1: reasoning        — "User is asking about order #444."        ← leaked from prior turn
2: tool_call        — get_order(order_id="444")
3: tool_observation — order 444: customer_name mismatches current user  ← contradiction visible
4: reasoning        — "Status is shipped."                      ← reasons past the mismatch
5: final_answer     — "Your order #444 has shipped."
```

- `first_suspicious_step: 1`, `first_unrecoverable_step: 1`,
  **`missed_recovery_step: 3`** — the observation contains concrete in-trace
  evidence (name mismatch) sufficient to detect the mixup; the agent reasons
  past it at step 4 (§2.3 satisfied: new contradicting information was
  present). `harmful_action_step: null` (read-only; no irreversible
  external effect), `visible_failure_step: 5`.
- `failure_category: "context.leakage"`, with
  `reasoning.ignored_evidence` as a contributing category (step 4).

## 6. Ambiguous cases

### 6.1 `first_suspicious_step == first_unrecoverable_step`

Legitimate whenever the first observable anomaly *is* the commitment (no
separate "smell" preceded it) — e.g. §5.2, where the invented assumption
appears and commits in the same reasoning step. Judges must not invent an
earlier suspicious step to keep the markers distinct. The same applies to
other coincidences (e.g. unrecoverable == harmful in §5.1). Auditors should
treat coincident markers as fine *when the evidence supports them* and as a
red flag only when a multi-step causal chain was plausibly collapsed
(especially unrecoverable == visible, §2.6).

### 6.2 `missed_recovery_step`: restatement vs. recovery opportunity

`refund_run_001` step 5 ("45 < 90 per policy; proceeding") looks like a
checkpoint — shouldn't the agent have re-verified the policy there?

**v1 interpretation: no.** A missed recovery point requires *new in-trace
information contradicting the committed path* (§2.3). Step 5 only re-applies
the step-2 commitment to the order data; nothing new contradicts the policy
choice between steps 2 and 5 (the v2 document was visible at step 1, *before*
the commitment — that's what makes step 1 suspicious, not step 5 a missed
recovery). Hence `missed_recovery_step: null` for that run. Rationale: if
restatements counted, every step after the commitment would qualify and the
marker would carry no information. Contrast §5.3, where a genuinely new
contradicting observation arrives after the commitment and the marker is set.

### 6.3 Categorization ties: picking the primary `failure_category`

When a run plausibly fits multiple categories (e.g. `rag.stale_source` vs.
`reasoning.unsupported_claim` for `refund_run_001`):

**Rule: the primary category describes what went wrong at
`first_unrecoverable_step`, characterized by its earliest in-trace cause.**

Tie-breakers, in order:

1. **Causal depth** — if one candidate explains the other, pick the
   explainer. In `refund_run_001` the reasoning was wrong *because* the stale
   source was used → `rag.stale_source`, not `reasoning.*`.
2. **Counterfactual** — which single fix at the critical step prevents the
   failure? (Right source precedence at step 2 prevents everything.)
3. **Evidence strength** — if still tied, pick the category with the
   strongest verifier-aligned evidence, and note the tie in
   `causal_explanation`.

Other plausible categories are contributing categories: name them in
`causal_explanation` (and in `contributing_failure_categories` once TRA-10
adds it). Do not encode multiple categories into the primary string.

## 7. Mapping to verifier evidence & failure-card fields

`Evidence.kind` values are from TRA-22: `step_ref`, `state_path`, `doc_span`,
`tool_arg`, `text`. Failure-card field names follow the Notion failure-card
definition (schema finalized in TRA-11).

| `AttributionResult` field | Typical `Evidence.kind` backing it | Failure-card field |
|---|---|---|
| `task_success` | — (copied from `VerifierResult.verifier_passed`; never contradicts it) | verifier outcome |
| `failure_category` | `doc_span` / `tool_arg` / `step_ref` (whatever grounds the cause) | failure category |
| `first_suspicious_step` | `step_ref` (+ `doc_span` for retrieval anomalies) | (timeline marker) |
| `first_unrecoverable_step` | `step_ref` (+ `doc_span`/`state_path` grounding the commitment) | first unrecoverable step |
| `missed_recovery_step` | `step_ref` pointing at the contradicting in-trace information | (timeline marker) |
| `harmful_action_step` | `step_ref` + `tool_arg` (the offending call/args) | (timeline marker; harm summary) |
| _(subsequent harm / false durable record)_ | extra `step_ref` entries with explanatory `note` (§2.5) | verifier evidence |
| `visible_failure_step` | `step_ref` (usually the `final_answer`/`message`) | visible symptom |
| `causal_explanation` | narrative; must reference the evidence entries | failure narrative |
| `evidence` | the list itself; overlaps `VerifierCheck.evidence` where verifier already proved the point | verifier evidence |
| `suggested_guardrail` | — (derived from the critical step) | suggested guardrail |
| `regression_test_idea` | — (derived from the critical step) | regression test idea |

Conventions: attribution evidence should *reuse* pointers the verifier
already established where possible (same `step_id`/`pointer` values as
`VerifierCheck.evidence`) so failure cards can cross-link them; every non-null
step marker must have at least one `Evidence` entry referencing that
`step_id`.

## 8. Versioning & change process

This is **taxonomy v1**, scoped to Month-1 support-agent workflows
(`tool_api`, `rag`). Expected evolution: new subcategories as the task bank
(TRA-14) grows; possible new top-level categories (e.g. human-interaction
failures) flagged as future work in Terminology & Concepts;
`contributing_failure_categories` via TRA-10.

Changes follow the project convention for shared contracts:

1. Propose the change on a Linear issue (taxonomy changes affect judge,
   failure cards, dashboard, and task specs — notify dependent streams via a
   Linear comment).
2. Update this file and the canonical Notion pages together (Katharine
   maintains canonical docs and the decision log).
3. Renames of existing dotted values are breaking changes for stored
   attributions and `failure_modes_targeted` lists; prefer additive changes.
   If a rename is unavoidable, record old → new in a mapping table in this
   section.
