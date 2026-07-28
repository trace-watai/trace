# TRACE Project State and Integration Review — July 28, 2026

**Reviewed code baseline:** `main` at `6693d849a020eedc7279c54e354eb65922f178bb`  
**Overall verdict:** The separate core workstreams are now integrated on `main`, the complete offline loop is green, and there are no open pull requests. The project is **not ready to declare complete or ship** until the remaining live-run, task-suite, real-data dashboard, bundle/audit, and clean-checkout acceptance evidence is retained.

## Macro issues to resolve first

1. **The task catalog is not yet one runnable product suite.** There are useful refund scenarios, but several families are placeholders or are not fully connected to an executable script, expected verifier result, contrasting positive case, suite manifest, and retained run. Emily’s new work is to turn the catalog into one coherent acceptance suite; Evan He should support her with controlled one-factor variants rather than build a separate suite.
2. **The live model path needs one final proof.** The Gemini implementation is now integrated and tested offline, but no one has yet run the final merged code with a real team API key and retained the sanitized trace, verifier result, artifacts, and run-index evidence. Rupert owns this proof.
3. **The dashboard still demonstrates sample data, not the product’s retained run.** The visible failure card is a solid first slice, but it must load the canonical fixture and then the actual runner output. Skye’s next work must connect the UI to the shared artifact contract so the dashboard cannot drift into a second data model.
4. **The full bundle and audit work must be repeated against integrated `main`.** Samir’s runnable bundles and Katharine’s audits need to consume the current task, runner, verifier, artifact, and dashboard contracts—not earlier branch snapshots.
5. **There is no final clean-checkout acceptance record.** The code gates pass locally, but Month 1 completion requires Evan Yang to run the complete acceptance matrix after the upstream work lands and Sarp to make an evidence-based readiness decision.

The practical consequence is simple: **stop starting parallel features. Finish the evidence chain through downstream consumers.**

## What was integrated and fixed

Nine worthwhile pull requests were brought onto current `main`. Where a contribution was close but unsafe or stale, I repaired it before merging instead of leaving integration work for the original owner.

| Integrated capability | What it now does | Integration work completed |
| --- | --- | --- |
| Full repository gate ([PR #122](https://github.com/trace-watai/trace/pull/122)) | Runs formatting, lint, all backend tests, and a pipeline smoke test as one gate | Repaired verifier fixtures and expectations that had drifted across environment, state, retrieval, tool, failure-bundle, and severity contracts |
| Required escalation in task definitions ([PR #112](https://github.com/trace-watai/trace/pull/112), Emily) | Lets a task explicitly say that a missing-information case must be escalated | Merged with schema validation and task-authoring guidance |
| First visible failure card ([PR #119](https://github.com/trace-watai/trace/pull/119), Skye) | Shows category, severity, affected steps, and blast radius in the dashboard | Merged after confirming its types and tests matched the backend contract |
| End-to-end escalation behavior ([PR #117](https://github.com/trace-watai/trace/pull/117), Evan Yang) | Exposes the escalation tool, verifies when it is required, and carries the result into the failure card | Fixed cross-layer schema and fixture mismatches and completed the verifier rules and coverage |
| Executable regression replay ([PR #120](https://github.com/trace-watai/trace/pull/120), Samir) | Converts a failure into a pinned replay that can prove the harmful case is blocked while a legitimate control still passes | Fixed integration with the current command line, guardrails, task fixtures, and artifact contract |
| Honest multi-task execution ([PR #107](https://github.com/trace-watai/trace/pull/107), Rupert) | Runs a suite and reports requested, passed, failed, terminated, and errored tasks without hiding partial failures | Corrected summary/accounting behavior and integrated it with the current pipeline |
| Complete deterministic dashboard fixture ([PR #104](https://github.com/trace-watai/trace/pull/104), Samrath) | Provides all 11 artifacts representing a complete failed refund run for offline UI and contract work | Rebuilt stale payloads for current schemas, added the escalation failure, expanded contract tests, and made repeat generation byte-for-byte stable |
| Current interface compatibility matrix ([PR #121](https://github.com/trace-watai/trace/pull/121), Justin) | Records which producer/consumer boundaries are proven and which still require live evidence | Rewrote it against the integrated repository instead of preserving obsolete branch assumptions |
| Live Gemini adapter ([PR #123](https://github.com/trace-watai/trace/pull/123), Rupert) | Converts Gemini responses into the same actions used by the offline runner and preserves provider evidence | Replaced the retired default model, disabled automatic SDK tool execution, rejected ambiguous multiple tool calls, corrected event ordering, persisted the effective configuration, passed suite settings through, and added offline SDK contract coverage |

The Gemini default is now `gemini-3.6-flash`; Google’s [official deprecation schedule](https://ai.google.dev/gemini-api/docs/deprecations) shows the former Gemini 2.0 Flash default as shut down. The adapter’s installed SDK shape was checked locally, but **no real API call was made** because the final key-backed acceptance belongs in a retained team run rather than an undocumented local smoke test.

## What the product can do now

From one repository command, TRACE can:

- load a refund task, initial environment, scripted or model-driven agent, and pinned policy documents;
- execute validated tools and record structured events and final state;
- verify objective refund-policy, evidence-grounding, stale-source, and escalation behavior;
- identify the likely root-cause, missed-recovery, irreversible-action, and symptom steps;
- generate a failure card, repair package, and executable regression artifact;
- retain and list runs through the normal artifact/index read path;
- run multiple tasks with honest totals;
- render the first failure-card experience in the dashboard;
- use the same runner with a Gemini-backed model adapter.

It cannot yet truthfully claim:

- broad runnable coverage across all promised refund task families;
- a successful retained live Gemini evaluation from final `main`;
- a dashboard driven end to end by a real retained run;
- five current, audited failure bundles;
- Month 1 release readiness from a clean checkout.

## Fresh verification evidence

All checks below were rerun on July 28 against `6693d849`:

- Backend repository gate: Ruff lint passed, Ruff formatting passed for 76 files, **379 tests passed**, and the pipeline smoke run passed.
- Dashboard gate: formatting, lint, type checking, **30 tests**, and production build all passed.
- The only dashboard build message was a non-blocking Next.js warning that `/Users/mo` and the dashboard both contain lockfiles; this should eventually be cleaned up or configured explicitly, but it did not affect compilation or tests.
- The deterministic full fixture contains **11 artifacts**; two fresh generations produced identical hashes.
- The offline product demo completed two indexed runs:
  - the harmful refund case failed on required escalation, unauthorized cash refund, unsupported outage claim, and reliance on a deprecated policy; it produced attribution, failure-card, repair, and regression artifacts;
  - the matched legitimate cash-refund case passed, demonstrating that the verifier does not merely block all refunds.
- GitHub shows **zero open pull requests**.
- The only untracked repository file is the pre-existing July 20 review document; it was preserved and not mixed into code changes.

## Branch and pull-request disposition

There is no useful current feature waiting in an open pull request. The remaining remote-only branches fall into four categories:

- older copies of data-contract, runner, mock-environment, trace-schema, run-index, and verifier work that already exists in newer form on `main`;
- the original Gemini branch, superseded by the corrected adapter now on `main`;
- older research or repair branches whose relevant findings have been incorporated into the current compatibility matrix and tickets;
- an `entire/checkpoints/v1` checkpoint branch, which is a snapshot rather than new integration work.

They should not be merged as a batch. Doing so would reintroduce obsolete schemas or duplicate implementations. They can be archived later after their owners confirm they have no personal notes to recover.

## Linear reset completed

The tracker now reflects the product rather than the old branch queue:

- The project target moved from July 28 to **August 4** and is marked **At Risk** with the remaining evidence gates.
- The canonical [Month 1 Start Here document](https://linear.app/trace-watai/document/trace-month-1-start-here-focus-order-8e58bfe54002) was fully rewritten with the current verdict, five gaps, owner table, dates, integration rules, closure order, and final acceptance gate.
- Cross-stream interface review and operating-cadence setup were closed as complete.
- Current due dates and integration comments were added to the task-suite, dashboard, bundle, audit, compatibility, full-acceptance, checkpoint, and readiness work.
- Two specific new tickets were created:
  - [TRA-80 — Turn the refund task families into one runnable acceptance suite](https://linear.app/trace-watai/issue/TRA-80/turn-the-refund-task-families-into-one-runnable-acceptance-suite), assigned to Emily, urgent, due August 1. It specifies exact coverage, scripts, expected results, positive siblings, manifest, coverage table, retained evidence, collaborators, and downstream consumers.
  - [TRA-81 — Run final-main Gemini acceptance and retain the complete TRACE evidence chain](https://linear.app/trace-watai/issue/TRA-81/run-final-main-gemini-acceptance-and-retain-the-complete-trace), assigned to Rupert, urgent, due July 30. It specifies the exact final-main run, configuration and ordering evidence, secret-safety checks, artifact chain, reviewers, and handoff.

## Closure plan

1. **July 30 — live path:** Rupert completes the retained Gemini run. Samrath checks trace/index evidence, Karan checks verifier evidence, and Justin updates the compatibility result.
2. **July 31 — real consumers and audits:** Skye lands canonical fixture loading and the run header. Katharine finishes the artifact/read-path and verifier/frontend audits, recording every finding as resolved or as a focused blocker.
3. **August 1 — coherent task coverage:** Emily and Evan He finish the runnable refund suite, with matched positive cases and no wording-only duplicates.
4. **August 2 — executable evidence:** Samir produces the current runnable bundles. Skye connects the failure summary to canonical artifacts.
5. **August 3 — inspectability:** Skye completes trace and failure drill-down from the retained artifact contract.
6. **August 4 — acceptance and decision:** Evan Yang runs the complete matrix from a clean checkout. Sarp confirms the integrated demo, records limitations, and makes the readiness call.

No ticket should be called done solely because its own files or tests pass. Its output must be read by the next component in the chain.

## Ready-to-send messages

### Emily

> Your earlier task-schema work is merged and is now part of the integrated product. I created TRA-80 for your next priority, due August 1: turn the existing refund families into one runnable acceptance suite. Please work from current main and coordinate with Evan He. Each counted scenario needs a task, executable script, exact expected verifier result, a contrasting positive case where overblocking is possible, a suite entry, and retained run evidence. The goal is not more JSON files; it is one suite that Samir’s bundles and Evan Yang’s final acceptance can run without private setup.

### Evan He

> Please focus TRA-44 on supporting Emily’s single acceptance suite, due August 1. Add variants that change one causal factor at a time—approval, purchase age, outage evidence, source status, refund type, or missing information—so we can explain exactly why the outcome changes. Avoid creating a second manifest or parallel task set. Review Emily’s coverage table for factor isolation and add matched positive controls wherever a verifier rule could overblock.

### Rupert

> Your suite runner and Gemini adapter are now merged into current main. I corrected integration details including the retired default model, multiple-tool-call handling, event order, effective configuration, and SDK execution settings. Your remaining priority is TRA-81, due July 30: run one controlled refund case from a fresh checkout of final main using the team key, then retain the sanitized run ID, command, configuration, trace ordering, verifier result, artifact paths, and run-index proof. Do not include the key. This closes the gap between “the adapter is implemented” and “the TRACE product works live.”

### Skye

> Your first visible failure card is merged and the dashboard gate is green. The next sequence is TRA-74, then TRA-29, then TRA-31: load the canonical 11-artifact fixture and show the run summary, connect the failure card to that data, and finally add trace/failure drill-down and real-run loading. Please do not copy the sample into another UI-only shape; the dashboard must read the same artifacts the runner writes. Target July 31 for loading, August 2 for the real failure summary, and August 3 for drill-down.

### Samir

> Your executable pinned regression work is merged and now proves both that a harmful behavior is blocked and that a legitimate control still passes. Please finish TRA-40 by August 2 using the integrated suite and artifact contracts on main. Produce the first complete runnable failure bundles without manual assembly, and record the task, run, verifier, attribution, failure-card, repair, and regression links for each case. Coordinate with Emily so her suite is the source, not a duplicate set.

### Katharine

> Please complete TRA-68 and TRA-69 by July 31 against current main. Audit the full producer-to-consumer chain: verifier evidence into failure card and frontend, plus trace and run-index artifacts into the dashboard read path. For every issue, say which producer emitted it, which consumer broke, the exact reproduction, and whether it is fixed or has a focused owner. Please sample Emily’s suite for ambiguity and answer leakage as it lands.

### Justin

> Your compatibility matrix is merged and was refreshed against the integrated repository. Please use TRA-64 as the live integration-risk log through August 4. The unresolved rows should now be limited to the retained Gemini run, coherent task suite, real-artifact dashboard, current bundles/audits, and clean-checkout acceptance. After Rupert’s run, record the actual result rather than assuming provider readiness from offline tests.

### Evan Yang

> Your escalation work is merged after the task, tool, verifier, fixture, and dashboard contracts were aligned. Please prepare TRA-52 for the August 4 final acceptance run, but execute final sign-off only after Emily’s suite, Rupert’s live evidence, Skye’s read path, and the bundle/audit work land. Run from a clean checkout, capture exact commands and outcomes, and prove both the failing and legitimate paths through every downstream artifact.

### Sarp

> The project has moved from July 28 to an August 4 integration target and is marked at risk, not failed. Core code is integrated and green; the remaining risk is evidence completion. Please use TRA-50 for daily blocker/demo review and TRA-51 for the final decision. Require five proofs: runnable task suite, retained live Gemini run, dashboard reading canonical artifacts, current bundles/audits, and clean-checkout acceptance. Do not close readiness based on ticket count.

### Samrath

> Your full dashboard fixture is merged after being refreshed to the current schemas; it now has all 11 artifacts, escalation evidence, stronger contract tests, and deterministic regeneration. Please review Rupert’s live trace and run-index evidence for TRA-81, and help Skye ensure the dashboard loader accepts the same artifact shapes. The fixture should remain the offline reference, while the real run proves the live path.

### Karan

> The canonical verifier and escalation behavior are integrated and the harmful/positive demo pair passes as expected. Please review Emily’s suite coverage so every expected outcome maps to existing deterministic checks, and review Rupert’s live verifier evidence. If a scenario needs a new verifier capability, create one focused blocker instead of allowing the suite to claim unsupported coverage.

### Darrel

> The integrated failure demo now retains attribution through the full artifact chain. Please review attribution on representative cases from Emily’s suite and Samir’s bundles, especially root cause, missed recovery, first irreversible action, and symptom steps. Confirm that evidence references resolve to the retained trace rather than relying on narrative-only explanations.

### Mohammed

> Keep the rewritten Start Here page as the only coordination source, check the five evidence gates daily, and prevent new side features until the August 4 decision. Escalate missing evidence early. The team’s success metric is one coherent product run that every downstream component can consume, not the number of merged tickets.

## Meeting script — what to say and emphasize

You can say this almost verbatim:

> “The good news is that the project is no longer a pile of separate branches. I reviewed the current code, every pull request, the remaining remote branches, and Linear. I repaired the worthwhile work that did not quite fit, merged it, and reran the complete checks. We now have one integrated main branch with no open pull requests.”
>
> “Today the offline product works end to end. It can load a refund task, run the agent and tools, save the trace and final state, deterministically identify policy and evidence failures, explain where the failure began, create the failure card and repair package, generate an executable regression test, and list the retained run. The harmful refund case fails for four precise reasons, while the legitimate comparison case passes. All 379 backend tests and all 30 dashboard tests pass, and both production gates are green.”
>
> “I want to be equally clear about what is missing. We are not done and should not call this ready to ship. We still need one coherent runnable suite across the promised refund scenarios, one retained real Gemini run from the final code, a dashboard that reads the real artifacts instead of sample data, current bundles and audits, and one clean-checkout acceptance run.”
>
> “The biggest rule for this week is that nobody should build another isolated version of their component. Emily and Evan He are building one task suite together. Rupert’s live run must go through the same runner, tools, verifier, artifacts, and index as the offline run. Skye’s dashboard must read those exact artifacts. Samir’s bundles must come from that suite. Katharine’s audits must test those connections. Evan Yang and Sarp only sign off after all of those pieces feed into each other.”
>
> “The dates are deliberate: Rupert’s live evidence by July 30; Skye’s loader and Katharine’s audits by July 31; Emily and Evan He’s suite by August 1; Samir’s bundles and the real failure summary by August 2; dashboard drill-down by August 3; final clean-checkout acceptance and readiness decision by August 4.”
>
> “For Emily specifically: you now have a detailed urgent ticket. We need complete runnable scenarios with scripts, expected verifier outcomes, positive comparison cases, one suite manifest, a coverage table, and retained evidence—not more placeholder task files.”
>
> “For everyone: a ticket is not done when your own files work. It is done when the next person’s component can consume the output from current main. In every handoff, include the exact command, commit, result, and artifact location. If something cannot be consumed downstream, flag it immediately instead of creating a private workaround.”
>
> “Our August 4 decision is evidence-based. If all five gates are retained and linked, we can call Month 1 complete. If even one is missing, we will state exactly what works, what does not, who owns it, and the next date. The priority is a coherent TRACE product, not a high count of completed tickets.”

