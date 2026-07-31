# retrieval_completeness family — deferred (TRA-80)

## TODO

This family is **not implemented**. It is deliberately excluded from
`fixtures/suites/refund_v0.json` and from the coverage table in
`docs/acceptance/refund-v0-suite.md`.

**Scenario intent:** an agent that skips retrieval entirely, or acts on an
incomplete/partial retrieval, should be caught even when it happens to land on
the correct final action — i.e. correctness of outcome should not excuse
ungrounded reasoning.

**Why it's blocked:** `RefundPolicyVerifier` loads policy rules directly from
environment state (`_load_rules`, keyed on the current doc's `metadata.rules`),
not from what the agent actually retrieved via `search_docs`. There is
currently **no check** that the agent consulted the current policy before
acting — an agent that never calls `search_docs` and still resolves the case
correctly passes today. See `src/trace_harness/verifiers/refund_policy.py`.

**What's needed before this family can be authored:**
1. A new deterministic verifier check (owner: Karan) — e.g.
   `policy_not_consulted` or `incomplete_retrieval` — that inspects
   `RETRIEVAL_RESULT` trace events (already emitted; see
   `_retrieval_provenance` in the verifier) and fails when the current policy
   doc was never retrieved, or a `top_k`/query choice omits it.
2. A linked Linear issue for that check (per TRA-80's integration rule:
   "if a scenario requires a verifier capability that does not exist, link a
   focused Karan issue before claiming the scenario accepted").

Once the check exists, this family should follow the same pattern as
`purchase_age`/`outage_evidence`: a positive control (partial retrieval that
still happens to reach the right answer → should now FAIL) and its matched
positive sibling (full retrieval → PASS), each with a task, script, and pinned
`fixtures/expected/*_expected_verifier.json`.
