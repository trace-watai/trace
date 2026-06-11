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

The failure, pinned and rerunnable: initial state and docs *as the failing
run saw them* (snapshots, not live fixtures — fixtures may evolve), the
failed check ids as the assertion set, severity, `blocks_release`, a
replay command, and **positive sibling tests**.

Positive siblings are the anti-overblocking mechanism and they are
mandatory in spirit: a fix for "unauthorized refund at 47 days" that also
blocks the legitimate 12-day refund is a new bug. The sibling
(`refund_policy_valid_cash`) must keep passing in the same CI gate that
replays the failure.

MVP honesty: `replay_command` re-runs the originating fixture;
`trace-harness replay <artifact>` (consuming the pinned state directly) is
the named next step in the regression section of `docs/modules.md`.

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
