# Verifier philosophy

**The verifier decides pass/fail. The judge explains why.**

This single sentence is the architecture. Everything release-blocking in
TRACE is decided by deterministic code over recorded evidence; LLMs help
with classification, summarization, and repair proposals — never with the
verdict.

## Why deterministic verdicts

1. **Reproducibility.** The same run verifies identically forever. A
   regression gate that flickers is worse than none.
2. **Auditability.** `unauthorized_cash_refund` failing means a specific
   refund record, a specific order field, a specific rule value — a human
   can re-derive the verdict from the evidence by hand (Katharine's audit
   path depends on this).
3. **Adversarial robustness.** An agent (or a prompt change) cannot talk a
   rule out of firing. A persuasive trace fools an LLM judge; it cannot
   change `purchase_age_days=47 > 30`.
4. **Cost and speed.** Verification runs in CI on every change for free.

This is also the research position (Justin): hard-correctness evaluation
should be oracle-based; model-graded evaluation drifts with the grader and
inherits its blind spots.

## What verifiers judge: state, not story

Verifiers read **what actually happened** — final state side effects,
trace events — never the agent's account of it. The refund verifier checks
the refunds list, not whether the agent said "I followed policy". The
fixture makes this vivid: the failing agent's final answer is *truthful*
about its unauthorized refund, so the consistency check passes while the
authorization check fails. Honesty and compliance are different checks.

## Rules are data, not opinions

`RefundPolicyRules` loads from the current policy doc's `metadata.rules` in
the run's own state — the verifier judges against the same pinned policy
the agent retrieved. Hardcoded thresholds in verifier code are a smell;
they drift from the fixtures and silently judge agents against a policy
nobody saw. (Tested: a stricter pinned rule set changes the verdict.)

## Overblocking is a verifier bug

A verifier that fails legitimate behavior poisons everything downstream:
false failure cards, bogus regressions, and a team that learns to ignore
red. Every blocking check ships with positive tests — the boundary cases
(day 30 passes, day 31 fails), the alternate authorization paths (manager
approval, store-credit-with-outage), and the subtle ones (mentioning a
deprecated doc while correctly rejecting it must pass). The
`refund_policy_valid_cash` fixture exists solely to keep this honest.

## Graded structure, not a bare boolean

`VerifierResult` carries failed checks (each with expected/actual,
severity, `blocks_release`, evidence with step ids) and warnings. Two
consequences:

- **Severity is per-check**, and `blocks_release` is a deliberate flag:
  the deprecated-authority check is diagnosis-grade (high severity, does
  *not* block — the refund check already blocks) so the same root cause
  isn't double-counted at the gate.
- **Checks that cannot run become warnings**, not crashes or silent
  passes: no provenance in the trace → "cannot assess source authority",
  stated explicitly.

## Where LLM judges fit (later)

A judge may: categorize failures, propose repair language, draft causal
narratives, triage which runs deserve human audit. A judge may not: flip
`passed`, change `blocks_release`, or suppress a failed check. When judge
and verifier disagree, the verifier is right by definition and the
disagreement is a research datapoint (Darrel + Justin's labeled set).

## Semi-deterministic territory

Some checks need judgment calls today (keyword claim-matching for ticket
grounding, final-answer consistency heuristics). The rule: heuristics must
be **documented in the check's code, conservative, and warning-biased** —
and their known failure modes get TODO'd toward structured solutions
(citations in traces, structured final answers), not buried.
