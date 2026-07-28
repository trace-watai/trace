# TRACE Project State and Integration Review — July 28, 2026

**Reviewed baseline:** `main` at `c2d740cd5dcdad46b56712b86c7482b1c105e100`

**Current verdict:** The integrated offline product works, is protected by required repository-wide checks, and now has a clean-checkout acceptance record. TRACE is still **not ready to call shipped** until the retained live Gemini run, broader runnable task coverage, five distinct failure bundles, arbitrary retained-run dashboard path, final audits, and named human sign-offs are complete.

## Macro issues at a glance

1. **Live use is not yet proven.** The Gemini connection is implemented and tested without a key, but nobody has retained a real provider run from final `main`. A real team credential is required, so this cannot be completed honestly in an uncredentialed integration session.
2. **The current suite is a strong starter, not the promised coverage.** Five meaningful outcomes run together, but only one is a failing case. Purchase age, approval, outage evidence, source ordering and retrieval, customer wording, and refund-type boundaries still need runnable cases and matched controls.
3. **The dashboard reads real generated artifacts, but only one committed run bundle.** The summary and failure card no longer use hand-maintained sample objects. The remaining product work is selecting arbitrary retained runs, showing trace/failure detail, and handling incomplete runs clearly.
4. **One complete bundle is not five distinct failure bundles.** The full artifact chain is proven for the harmful refund case. Four more bundles must come from distinct runnable failures and pass cross-artifact consistency checks.
5. **Technical checks cannot replace human acceptance.** The repository gate is complete, but the named platform, consumer, audit, and readiness reviewers still need to record their decisions.
6. **Some dependency findings are upstream compatibility work.** The safe patch upgrades are merged. Eleven high audit nodes remain behind two supported-version boundaries; they must not be hidden with forced fixes, downgrades, or unsupported overrides.

## What was completed and merged

The integration top-off added five focused changes after the earlier component work was brought together:

| Product outcome | What changed |
| --- | --- |
| **A real shared quality gate** ([PR #127](https://github.com/trace-watai/trace/pull/127)) | Every pull request and every update to `main` now runs the full backend gate and the full dashboard gate. The checks are required and strict, so a branch must be current before merging. Force pushes and branch deletion are disabled. |
| **A canonical five-outcome suite** ([PR #128](https://github.com/trace-watai/trace/pull/128)) | One command now runs a harmful unauthorized refund, a valid cash refund, valid store credit, a correct refusal, and a missing-information escalation. The expected result is four passes and one intentional failure, with honest totals and no hidden errors. |
| **A dashboard backed by generated product artifacts** ([PR #129](https://github.com/trace-watai/trace/pull/129)) | The page loads all 11 artifacts from a generated TRACE run, validates their shared identity and evidence links, and renders the result from that contract. The duplicate UI-only sample was removed. |
| **Current CI runtimes** ([PR #130](https://github.com/trace-watai/trace/pull/130)) | GitHub actions were moved to their current major versions and dashboard CI moved to Node 24, eliminating the old-runtime warnings. |
| **The complete run summary** ([PR #131](https://github.com/trace-watai/trace/pull/131)) | The dashboard header now includes the task title and blast-radius summary required by the accepted run-summary ticket. |

The safe dependency patches in the first change moved Next.js and its ESLint configuration to 15.5.22, PostCSS to 8.5.24, and corrected compatible lockfile dependencies. No unsafe forced audit fix was used.

## Fresh evidence from final main

- Final Integration CI passed on `c2d740cd`: [workflow run](https://github.com/trace-watai/trace/actions/runs/30404984521).
- Backend gate: Ruff checks, **380 tests**, repository smoke test, harmful demo, and valid comparison all passed.
- Dashboard gate: format, lint, type checking, **33 tests**, and production build all passed.
- A clean clone passed the full backend and dashboard matrix. The retained record is [2026-07-28-clean-checkout.md](acceptance/2026-07-28-clean-checkout.md).
- The five-outcome suite completed all five requests with four expected passes, one expected failure, zero terminations, and zero errors. Coverage and deferred families are documented in [refund-v0-suite.md](acceptance/refund-v0-suite.md).
- The dashboard loader validates all **11 artifacts** and **47 trace events**. Every verifier and failure-card evidence step resolves to the retained trace.
- A production dashboard start showed the expected run, task, status, verdict, severity, blast radius, and artifact count.
- There are no open pull requests. Remaining remote branches are stale or superseded snapshots, not additional current features waiting to be merged.
- GitHub issue #110 and Linear TRA-78 remain In Review only for the named human sign-off; the technical gate itself is complete and green.

## Exactly what remains

### 1. Retained live Gemini evidence — Rupert, TRA-81

From a fresh checkout, Rupert must use the team-owned key to run the valid refund scenario through the normal pipeline, then retain the sanitized run ID, effective settings, provider-response ordering, validated tool calls, verifier result, run-index readback, and artifact paths. A failure is useful evidence and must not be discarded in favor of repeated attempts.

The exact command and evidence checklist are posted on TRA-81 and GitHub issue #125. No key or local `.env` was available during this integration session, so no live result is being claimed.

### 2. Broader task coverage — Emily and Evan He, TRA-80 and TRA-44

The shared suite exists and works. Emily now needs to extend that same suite across the missing behavior boundaries. Every counted case needs an executable script, exact expected outcome, relevant verifier checks, and a matched positive or contrasting sibling where overblocking is possible. Evan He should add one-factor-at-a-time variants inside this catalog, not create a second suite.

### 3. Five distinct complete bundles — Samir, TRA-40

The harmful-refund run proves the complete bundle path. Samir must generate four additional distinct failing runs from the expanded suite. Each counted bundle needs aligned task/run identity, trace, verifier evidence, attribution, failure card, repair controls, executable regression, and representative dashboard parsing. Copies or wording variants of the current bundle do not count.

### 4. Arbitrary retained-run dashboard and drill-down — Skye, TRA-29 and TRA-31

The static loader and run summary are complete. Skye should reuse that loader to add trace and failure drill-down, then connect a run selector or equivalent adapter to the existing run index/read service. Missing or partial artifacts must produce explicit states rather than a blank page or invented data.

### 5. Audits and human decisions — Katharine, Sarp, Evan Yang, and named reviewers

Katharine must finish the verifier-to-card/frontend audit and the run/index-to-dashboard audit on current `main`. Sarp and the platform/downstream reviewers must finish TRA-78’s sign-off. Evan Yang must execute the final integrated acceptance event after the live and coverage evidence is retained, and Sarp must record the readiness decision.

### 6. Supported dependency resolution — Skye, TRA-82

The remaining audit output reduces to the Next.js/Sharp supported range and the Next ESLint/minimatch/brace-expansion graph. TRA-82 requires a supported upgrade path, full before/after evidence, and all product checks. If upstream still has no compatible release, the correct result is a documented exception and review date—not a forced green audit count.

## Why this was not already finished by individual members

The main failure was ownership at the boundaries, not a total absence of work. Contributors delivered useful schemas, tasks, runner behavior, verifier logic, artifacts, UI pieces, and the Gemini adapter, but most tickets proved only their own component. Several branches were based on older contracts, and no single ticket initially owned the complete path from task to runner to verifier to retained artifact to dashboard.

The final gaps also differ from ordinary implementation:

- a live run needs a real credential and retained team evidence;
- five bundles need several distinct runnable failures, not five folders;
- dashboard completion requires consuming the runner’s artifacts rather than rendering a local sample;
- audits and release readiness require named human judgment;
- dependency cleanup is constrained by supported upstream version ranges.

The correction is now explicit: a ticket is complete only when its downstream consumer exercises the result from current `main`.

## Tracker state and next ownership

The canonical Linear document, **TRACE Month 1 Start Here — Focus Order**, now reflects `c2d740cd`, the completed gates, and the remaining no-ship evidence.

| Owner | Next concrete outcome |
| --- | --- |
| Emily Au | Finish TRA-80 by extending the shared suite across the missing families with scripts, expected results, matched controls, coverage table, and retained evidence. |
| Evan He | Finish TRA-44 with factor-isolated variants inside Emily’s suite. |
| Rupert | Execute TRA-81 with the real team key and obtain the named trace, verifier, and compatibility reviews. |
| Skye | Complete TRA-29/31 for drill-down and arbitrary retained runs; track the supported dependency upgrade in TRA-82. |
| Samir | Finish TRA-40 with five distinct generated failure bundles. |
| Katharine | Complete TRA-68 and TRA-69 against final `main`, with every finding fixed or assigned. |
| Justin | Keep TRA-64’s compatibility and integration-risk view aligned with actual evidence. |
| Evan Yang | Complete TRA-52’s final integrated acceptance after upstream evidence lands. |
| Sarp | Finish TRA-78 human sign-off and make the TRA-51 readiness decision. |
| Mohammed | Hold scope, keep the canonical status current, and enforce downstream-consumer completion. |

## Ready-to-send messages

### Emily

> The shared five-outcome suite is merged and green, so you have a working foundation rather than a blank task. Please finish TRA-80 by extending that same suite across purchase age, approval, outage evidence, source ordering and retrieval, customer wording, refund type, and matched controls. Every case needs an executable script, exact expected result, verifier coverage, and retained evidence. Coordinate with Evan He and do not create a parallel catalog.

### Evan He

> Please use TRA-44 to support Emily’s one shared suite. Change one causal factor at a time so we can explain why an outcome changed, and add a positive sibling whenever a new control could block legitimate behavior. Review her coverage table for gaps instead of creating a second manifest.

### Rupert

> The Gemini adapter and the full integration gate are merged. Your remaining job is evidence, not more adapter code: run final `main` with the team key using the exact TRA-81 instructions, retain the first result honestly, prove provider response and tool ordering, verify the run through the normal read path, and obtain the named reviews. Never attach the key.

### Skye

> The dashboard now reads the full generated 11-artifact contract and the run summary is complete. Please build TRA-29 and TRA-31 on that loader: add trace and failure detail, then load arbitrary retained runs through the existing index/read service with clear missing-data states. TRA-82 is separate dependency maintenance; do not force or downgrade packages to clear the audit.

### Samir

> One complete failure bundle is proven, but TRA-40 still needs five distinct failures. Consume Emily and Evan He’s runnable suite and generate each bundle through the common pipeline. Validate that task, run, trace, verifier, attribution, failure card, repair, and regression artifacts all agree. Do not clone the current bundle to reach five.

### Katharine

> Please complete TRA-68 and TRA-69 against final `main`. Audit the actual producer-to-consumer chain: verifier evidence into cards and UI, and retained runs/index into dashboard reading. For every gap, give the reproduction, broken boundary, and either the fix or focused owner.

### Justin

> The technical integration gate is complete. Keep TRA-64 focused on the remaining evidence: real Gemini behavior, expanded suite coverage, five bundles, arbitrary-run UI, audits, and sign-off. Update the matrix from observed results, especially Rupert’s retained run.

### Evan Yang

> The clean-checkout baseline is proven. Your final TRA-52 event should happen after the live run, broader suite, bundle, dashboard, and audit evidence land. Run the complete integrated path from a fresh checkout and retain exact commands, outcomes, run IDs, and artifact locations.

### Sarp

> Main is technically protected and green, but readiness is still no-ship. Finish the named TRA-78 review and use TRA-51 for the final decision. Require the live run, broad suite, five bundles, arbitrary-run drill-down, audits, and final acceptance evidence; do not substitute ticket counts.

### Samrath

> The generated fixture is now consumed directly by the dashboard and all 11 artifacts are validated together. Please review Rupert’s live trace/index evidence and help confirm that the live bundle matches the offline reference contract.

### Karan

> Please review verifier coverage for Emily’s expanded suite and Rupert’s live result. Every claimed outcome must map to deterministic checks. If a scenario needs a new capability, create a focused blocker instead of approving unsupported coverage.

### Darrel

> Please review attribution across the new failing cases and confirm root cause, recovery opportunity, irreversible action, and symptom references resolve to the retained trace rather than narrative-only explanations.

## Meeting script — say this clearly

> “The project is now one integrated offline product, not a collection of separate branches. The shared checks are required, a clean checkout passes, the five-outcome starter suite runs, and the dashboard reads the complete generated evidence bundle. There are no open code reviews waiting to be merged.”
>
> “We still should not call it shipped. We need one retained real Gemini run, broader runnable refund coverage, five distinct failure bundles, a dashboard that can open any retained run and show its details, and the final audits and human approvals.”
>
> “The main problem before was that people completed their own component without one owner proving the handoff to the next component. That rule changes now. Work is done only when the next part of the product consumes it from the current shared branch.”
>
> “Emily and Evan He extend one shared task suite. Rupert runs that same product path with Gemini. Samir generates bundles from those actual failures. Skye reads those exact retained results. Katharine audits those connections. Evan Yang runs the final acceptance, and Sarp decides readiness from the retained evidence.”
>
> “Every handoff must include the exact version, command, result, run identifier, and artifact location, with no secrets. If a live run fails, keep it—it is evidence. If an upstream package cannot be safely upgraded, document the exposure rather than forcing a risky fix.”
>
> “The remaining gap is now specific execution and evidence, not missing architecture. Our August 4 decision is simple: if every required proof is linked, we can call Month 1 complete. If one is missing, we name that proof and its owner instead of calling the product shipped.”
