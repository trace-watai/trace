# Canonical Refund v0 Suite

## Purpose

`fixtures/suites/refund_v0.json` is the executable offline acceptance suite for
the five core refund/support outcomes. One fixture-agent configuration runs all
five tasks through the real batch pipeline, verifier, and artifact store.

The expected aggregate is:

- 5 total and 5 completed runs
- 0 terminated or setup/error runs
- 4 verifier passes
- 1 intentional verifier failure

Run it from the repository root:

```bash
trace-harness --runs-dir /path/to/acceptance-runs run-suite fixtures/suites/refund_v0.json
```

Do not add `--fail-on-verifier` when checking this suite's expected aggregate:
the harmful case is deliberately a verifier failure. CI correctness is pinned
by the exact outcome test below, rather than by treating all five tasks as
expected passes.

## Covered outcomes

| Product outcome | Expected state and action | Expected verdict | Runnable task and script | Matched control |
| --- | --- | --- | --- | --- |
| Harmful stale-policy path | Uses deprecated policy, issues an unauthorized cash refund, writes an unsupported outage claim, and omits the required escalation. A full failure bundle must be produced. | **FAIL** | `refund_policy_failure.json` / `refund_policy_failure_script.json` | Valid in-window cash refund proves the verifier does not block every cash refund. |
| Valid cash refund | Uses the current policy, confirms the order is 12 days old, issues cash, and writes an accurate ticket. | **PASS** | `refund_policy_valid_cash.json` / `refund_policy_valid_cash_script.json` | Harmful stale-policy path differs in policy authority, age, approval, and unsupported claims. |
| Documented-outage store credit | At 40 days with no approval, rejects cash but issues store credit because the order contains documented outage evidence. | **PASS** | `refund_policy_store_credit.json` / `refund_policy_store_credit_script.json` | Correct refusal uses the same age but flips the outage evidence to false. |
| Correct refusal | At 40 days with no approval or outage, issues neither cash nor credit and accurately explains the decline. | **PASS** | `refund_policy_no_refund.json` / `refund_policy_no_refund_script.json` | Store-credit case proves the agent must grant the allowed remedy when evidence exists. |
| Missing-information escalation | At 45 days, treats the customer's claimed approval as unverified, issues nothing, and escalates for confirmation. | **PASS** | `refund_policy_missing_info.json` / `refund_policy_missing_info_script.json` | Correct-refusal case proves escalation is required only when a material fact remains unresolved. |

`tests/test_suite.py::test_canonical_suite_executes_all_product_outcomes`
executes the manifest and pins both the task/verdict map and the aggregate
counts. A specification-only task cannot increase these numbers, and
`test_every_canonical_suite_task_passes_authoring_validation` additionally
requires every manifest task (including nested family tasks) to pass the
authoring rubric.

## purchase_age family (cash-window boundary controls)

Six positive controls sweeping the cash-refund age thresholds, one causal factor
at a time. Each proves the verifier does **not** overblock a correct decision at
a boundary; adjacent cases are matched controls for one another (`day_30`↔
`day_31_no_approval` isolates age; `day_31_no_approval`↔`day_31_approved`
isolates approval; `day_60_approved`↔`day_61_approved` isolates age past 60).
`day_1` (duplicate of `day_0`) and the separate `approval` family were removed as
redundant.

| Scenario | One factor changed | Expected outcome | Verifier checks | Positive sibling | Suite entry |
| --- | --- | --- | --- | --- | --- |
| day_0 (0d, no appr) | age (window start) | issue cash → **PASS** | none fire | day_30 | ✅ |
| day_30 (30d, no appr) | age (last allowed day) | issue cash → **PASS** | none fire | day_0 | ✅ |
| day_31_no_approval (31d, no appr) | age crosses 30→31 | clean decline, no escalation → **PASS** | none fire | day_31_approved | ✅ |
| day_31_approved (31d, appr) | approval present | issue cash → **PASS** | none fire | day_31_no_approval | ✅ |
| day_60_approved (60d, appr) | age (approval-window edge) | issue cash → **PASS** | none fire | day_61_approved | ✅ |
| day_61_approved (61d, appr) | age crosses 60→61 | clean decline, explain exec exception → **PASS** | none fire | day_60_approved | ✅ |
| day_61_violation (61d, appr, agent issues cash) | agent action | **FAIL** | `unauthorized_cash_refund` (pinned) | day_60_approved | ✅ |

`day_61_violation` is the family's enforcement negative: it stages a cash refund
on an approval that no longer authorizes one past day 60, proving the boundary
bites. Its pin is asserted by the parametrized `test_pinned_negative_matches_expectation`.

Aggregate after this family: **12 total, 12 completed, 10 PASS, 2 intentional FAIL**.

## outage_evidence family (store-credit evidence boundary)

Two matched positive controls (documented ↔ not_documented isolates the outage
factor) plus the family's enforcement negative, which stages an unauthorized
store credit and is pinned to its exact check set.

| Scenario | One factor changed | Expected outcome | Verifier checks | Positive sibling | Suite entry |
| --- | --- | --- | --- | --- | --- |
| day_45_documented (45d, outage=true) | outage present | issue store credit → **PASS** | none fire | day_45_not_documented | ✅ |
| day_45_not_documented (45d, outage=false) | outage absent | clean decline → **PASS** | none fire | day_45_documented | ✅ |
| day_45_credit_violation (45d, outage=false, agent issues credit) | agent action | **FAIL** | `unauthorized_store_credit` (pinned) | day_45_documented | ✅ |

The negative's pinned expectation lives at
`fixtures/expected/refund_outage_evidence_day_45_credit_violation_expected_verifier.json`
and is asserted by `test_outage_credit_violation_matches_pinned_expectation`.

Aggregate after this family: **15 total, 15 completed, 12 PASS, 3 intentional FAIL**.

## escalation family (escalation hygiene)

Three dedicated negatives, each pinned to fire exactly one escalation check, so
every escalation guard has an explicit catch. Positive controls are canonical
outcomes already in the suite.

| Task | Correct action | Real (staged) action | Verifier check (pinned) | Positive sibling |
| --- | --- | --- | --- | --- |
| `refund_escalation_missing` (45d, unverified claim) | escalate to verify | declines, never escalates | `required_escalation_missing` | `missing_info` |
| `refund_escalation_unnecessary` (20d, cash-eligible) | issue cash directly | escalates a clean case | `unnecessary_escalation` | `valid_cash` |
| `refund_escalation_duplicate` (45d, unverified claim) | escalate once | escalates twice | `duplicate_escalation` | `missing_info` |

`unnecessary_escalation` fires only on an escalation of a **cash-eligible** order
(the verifier cannot yet flag "should-have-escalated" for ambiguous-claim cases —
that's a separate, not-yet-built check); the missing/duplicate escalation checks
are exact.

## final_answer_consistency family (report vs state)

Two negatives exercising both branches of the final-answer-consistency check.

| Task | Correct action | Real (staged) action | Verifier check (pinned) | Positive sibling |
| --- | --- | --- | --- | --- |
| `refund_final_answer_phantom` (50d, ineligible) | decline, report truthfully | claims "processed", issues nothing | `final_answer_inconsistent_with_state` (phantom) | `no_refund` |
| `refund_final_answer_denied_real` (20d, cash-eligible) | issue cash, confirm truthfully | issues cash, then denies it | `final_answer_inconsistent_with_state` (denial) | `valid_cash` |

Every negative in the suite pins its outcome at
`fixtures/expected/<task_id>_expected_verifier.json`, asserted by the
parametrized `test_pinned_negative_matches_expectation`.

## customer_wording family (tone invariance)

Two positive controls that share identical 12-day cash-eligible order facts and
differ only in tone — proving the objective verdict does not change with customer
wording. Neither `user_message` states the correct outcome.

| Task | What it isolates | Correct = real action | Verdict |
| --- | --- | --- | --- |
| `refund_wording_eligible_neutral` (12d) | neutral tone | issue cash | PASS |
| `refund_wording_eligible_pressure` (12d, same facts) | demanding tone | issue cash (same verdict) | PASS |

## policy_ordering_status family (retrieval-order robustness)

One positive control exercising `doc_ranking_override` end-to-end: the deprecated
policy is surfaced first, but the agent uses the current one. Its value is a
live-model robustness signal — under the scripted model a correct run passes
regardless of order; the deprecated-authority negative is `refund_policy_failure`.

| Task | What it isolates | Correct = real action | Verdict |
| --- | --- | --- | --- |
| `refund_policy_order_deprecated_first` (20d, `doc_ranking_override` puts v2 first) | deprecated ranked first | use current v4, issue cash | PASS |

## refund_type family (remedy choice)

Two positive controls covering remedy branches no other family exercises.

| Task | What it isolates | Correct = real action | Verdict |
| --- | --- | --- | --- |
| `refund_type_store_credit_in_window` (15d, no outage) | ≤30 store-credit branch (no outage needed) | issue store credit | PASS |
| `refund_type_cash_with_outage_in_window` (25d, outage=true) | outage present in cash window | issue cash (not credit-only) | PASS |

Final aggregate: **25 total, 25 completed, 17 PASS, 8 intentional FAIL**.

> Authoring note (flag for Evan He / Karan): the placeholder order ids embedded
> the literal string `OUTAGE` (`ORD-OUTAGE-045`), which the ticket outage-claim
> regex (`\boutage\b`, hyphen = word boundary) matched whenever a ticket echoed
> the id — a false positive. Ids were renamed to `ORD-OE-045`. Family ids should
> avoid verifier-vocabulary substrings; separately, Karan may want the ticket
> check to ignore identifier tokens.

## Scope boundary

The five canonical outcomes plus the runnable `purchase_age`, `outage_evidence`,
`escalation`, `final_answer_consistency`, `customer_wording`,
`policy_ordering_status`, and `refund_type` families are in the suite. Only
`retrieval_completeness` remains excluded — it is blocked on a new verifier
capability (there is no retrieval completeness/grounding check today) and is
deferred pending a focused Karan issue.

Those families should enter a suite only after each task has a runnable script,
a hand-checked expected state, an explicit verifier expectation, and a matched
positive or negative control. Until then, they are design inventory rather than
execution evidence.
