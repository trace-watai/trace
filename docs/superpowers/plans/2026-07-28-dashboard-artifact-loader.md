# Dashboard Artifact Loader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the dashboard summary and failure card read the canonical generated 11-artifact refund fixture instead of a separate sample object.

**Architecture:** A server-only loader owns filesystem/JSONL reading and invokes the existing artifact parsers. It returns one aggregate fixture model. The server-rendered landing page consumes that model; components remain presentational.

**Tech Stack:** Next.js App Router, TypeScript, Node filesystem APIs, Vitest, existing TRACE artifact parsers.

## Global Constraints

- Do not invent a second wire format.
- Do not recalculate verifier severity or category in the UI.
- Do not claim arbitrary stored-run loading or complete drill-down.
- Use TDD: loader test must fail before implementation.

---

### Task 1: Define loader behavior test-first

**Files:**
- Create: `apps/dashboard/src/data/refund-failure-fixture.test.ts`
- Create later: `apps/dashboard/src/data/refund-failure-fixture.ts`

**Interfaces:**
- Consumes: the committed `src/fixtures/refund-failure` files and existing `parse*` functions.
- Produces: `loadRefundFailureFixture(): RefundFailureFixture`.

- [ ] **Step 1: Write the failing loader test**

The test imports `loadRefundFailureFixture` and asserts:

- run ID agrees across run result, verifier, attribution, failure card, repair, regression, and every trace event;
- status is `completed`, verifier verdict is false, severity is `critical`, and step count is 7;
- trace begins with `run_started` and ends with `run_finished`;
- every failed-check and failure-card evidence step resolves to a trace step;
- all 11 artifact names are represented.

- [ ] **Step 2: Verify RED**

Run: `npm run test:run -- src/data/refund-failure-fixture.test.ts`

Expected: module-not-found failure because the loader does not exist.

### Task 2: Implement the aggregate loader

**Files:**
- Create: `apps/dashboard/src/data/refund-failure-fixture.ts`

**Interfaces:**
- Consumes: raw JSON imports, `trace.jsonl`, and current parsers.
- Produces: parsed `runResult`, `verifierResult`, `attributionResult`, `failureCard`, `repairPackage`, `regressionArtifact`, `trace`, plus named raw task/config/state records and `artifactNames`.

- [ ] **Step 1: Add the minimum server loader**

Use `readFileSync(new URL("../fixtures/refund-failure/trace.jsonl", import.meta.url), "utf8")` for JSONL and direct JSON imports for other artifacts. Parse with existing functions.

- [ ] **Step 2: Verify GREEN**

Run: `npm run test:run -- src/data/refund-failure-fixture.test.ts`

Expected: the new test passes.

### Task 3: Replace sample UI data

**Files:**
- Modify: `apps/dashboard/src/app/page.tsx`
- Modify: `apps/dashboard/src/types/failure-card.test.ts`
- Delete: `apps/dashboard/src/data/sample-failure-card.ts`
- Delete: `apps/dashboard/src/data/sample-failure-card.json`

**Interfaces:**
- Consumes: `loadRefundFailureFixture`.
- Produces: artifact-backed run summary and existing failure-card rendering.

- [ ] **Step 1: Update the failure-card contract test**

Import the canonical fixture failure card instead of the duplicate sample JSON.

- [ ] **Step 2: Update the page**

Load the fixture once and display run ID, task ID, status/termination, steps, verifier verdict, and severity above `<FailureCard card={fixture.failureCard} />`.

- [ ] **Step 3: Remove duplicate sample files**

Delete both unused sample data files.

- [ ] **Step 4: Run the full dashboard gate**

Run:

```bash
npm run format:check
npm run lint
npm run typecheck
npm run test:run
npm run build
```

Expected: all commands exit 0.

- [ ] **Step 5: Run the backend integration gate**

Run: `PYTHONPATH=src PATH=.venv/bin:$PATH ./scripts/check_repo.sh`

Expected: producer-side contracts remain green.

- [ ] **Step 6: Commit, publish, and merge**

Open a PR, require both integration checks, merge, and verify the final page artifact source on `main`.
