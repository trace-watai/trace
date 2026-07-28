# Integration Closure Top-Off Design

**Date:** July 28, 2026  
**Approved direction:** Three independently reviewable pull requests, merged in order.

## Goal

Convert already-working TRACE components into enforced, visible integration evidence without pretending that the remaining live-provider, broad task-family, five-distinct-failure, or arbitrary stored-run dashboard work is complete.

## Verified starting point

- `main` at `2520c46` has no open pull requests.
- A fresh remote clone passes 379 backend tests, Ruff, pipeline smoke, the harmful/positive offline demo, 30 dashboard tests, dashboard format/lint/typecheck, and production build.
- Five canonical outcomes already execute through `BatchRunner`: one intentional harmful failure plus valid cash, store credit, correct refusal, and missing-information escalation.
- The dashboard renders a separate sample failure-card object even though a deterministic 11-artifact fixture is committed.
- GitHub runs only dashboard format/lint today; no backend workflow or required checks protect `main`.
- The dashboard lockfile has patchable direct Next.js/PostCSS advisories and additional transitive advisories. No forced breaking downgrade is acceptable.
- No `GEMINI_API_KEY` or repository `.env` is available, so a real provider call remains an explicit external gate.

## Pull request 1: Integration enforcement and dependency patch

Replace the narrow dashboard-only workflow with one integration workflow that runs on every pull request and on pushes to `main`.

The backend job installs the package with development dependencies, runs `scripts/check_repo.sh`, and runs `scripts/demo.sh` in an isolated temporary runs directory. The dashboard job installs from the lockfile and runs formatting, lint, type checking, all tests, and production build.

Update only safe patch-level dashboard dependencies. Do not use `npm audit fix --force`, downgrade Next.js, or force a transitive version outside an upstream-supported range merely to produce a zero count. Record remaining advisories explicitly if the supported dependency graph cannot remove them.

After a successful pull-request workflow and merge, configure `main` to require the two integration jobs if repository permissions allow it. Do not alter review-count or team-access policy.

## Pull request 2: Canonical five-outcome suite

Expand `fixtures/suites/refund_v0.json` from two tasks to the five existing canonical runnable tasks:

1. harmful deprecated-policy and unauthorized-refund failure;
2. valid in-window cash refund;
3. documented-outage store credit;
4. correct no-refund refusal;
5. missing-information escalation.

Pin the exact manifest membership and executable aggregate result in tests. Add a coverage table that distinguishes this five-outcome MVP suite from the broader unfinished task-family expansion. Do not claim that placeholder approval, customer-wording, policy-order, refund-type, or retrieval-completeness families are finished.

## Pull request 3: Artifact-backed dashboard summary

Remove the parallel sample failure-card data source. Add one server-side typed loader for the committed `refund-failure` fixture. It must parse the run result, verifier result, attribution, failure card, repair package, regression artifact, and trace with the existing domain parsers, and expose the remaining task/config/state artifacts as named raw records.

The landing page reads the loader and displays a run summary plus the existing failure card. Every displayed run ID, status, verdict, severity, step count, and failure-card field comes from the canonical generated artifacts.

This closes the static artifact-loading foundation. It does not claim arbitrary `runs/{run_id}` filesystem/API loading, loading/error states, or the complete trace drill-down promised by TRA-31.

## Error and security behavior

- CI commands fail normally; no `continue-on-error` on correctness gates.
- Suite totals must expose setup, termination, and verifier failures honestly.
- The fixture loader throws on malformed input; tests prove cross-artifact run IDs and trace evidence links agree.
- No secrets, API keys, or live-provider payloads are added to source or CI.
- Dependency remediation stays within safe patch updates unless a separately reviewed compatibility change is required.

## Verification and completion

Each pull request gets its narrow tests plus the full relevant repository gate. Each is merged before the next branch is created from updated `main`.

After all three merges:

- rerun GitHub/branch/PR state;
- rerun the full local backend and dashboard gates;
- verify the required GitHub checks;
- update Linear with exact commits, workflow runs, test counts, what can close, and what remains genuinely blocked;
- retain the no-ship verdict for the real Gemini run, unfinished broad task families/five distinct failure bundles, and arbitrary stored-run dashboard path.
