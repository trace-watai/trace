# Integration Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the already-green backend and dashboard gates execute automatically on every pull request and `main`, while applying safe dashboard dependency patches.

**Architecture:** One GitHub Actions workflow owns two independent jobs: backend integration and dashboard integration. Patch-level dependency updates are locked by npm and verified by the complete dashboard gate; unresolved upstream/transitive advisories are documented instead of hidden or force-fixed.

**Tech Stack:** GitHub Actions, Python 3.12, pip, Ruff, pytest, Bash, Node 20, npm, Next.js 15.5.

## Global Constraints

- Do not use `npm audit fix --force`.
- Do not run live Gemini calls in CI.
- Both CI jobs run on pull requests and pushes to `main`.
- Preserve the pre-existing untracked July 20 report in the primary checkout.

---

### Task 1: Safe dashboard dependency patches

**Files:**
- Modify: `apps/dashboard/package.json`
- Modify mechanically: `apps/dashboard/package-lock.json`

**Interfaces:**
- Consumes: the current Next.js 15.5 dashboard and npm lockfile.
- Produces: the latest compatible 15.5 patch dependency graph and an auditable remaining-risk result.

- [ ] **Step 1: Record the current audit**

Run: `npm audit --json`

Expected: non-zero with direct Next.js/PostCSS findings on the old lockfile.

- [ ] **Step 2: Patch supported direct ranges**

Set `next` and `eslint-config-next` to `^15.5.22`, and both the PostCSS dependency and override to `^8.5.24`. Regenerate the lockfile with `npm install`.

- [ ] **Step 3: Verify dashboard behavior**

Run:

```bash
npm run format:check
npm run lint
npm run typecheck
npm run test:run
npm run build
```

Expected: every command exits 0.

- [ ] **Step 4: Record the post-patch audit**

Run: `npm audit --json`

Expected: direct patched findings are gone. If supported transitive findings remain, record their exact package paths and do not force an incompatible version.

### Task 2: Full integration workflow

**Files:**
- Move/replace: `.github/workflows/dashboard-ci.yml` -> `.github/workflows/integration-ci.yml`

**Interfaces:**
- Consumes: `scripts/check_repo.sh`, `scripts/demo.sh`, and dashboard npm scripts.
- Produces: `Backend gate` and `Dashboard gate` status checks.

- [ ] **Step 1: Replace the scoped workflow**

Create this workflow shape:

```yaml
name: Integration CI

on:
  pull_request:
  push:
    branches: [main]

jobs:
  backend:
    name: Backend gate
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - run: python -m pip install -e ".[dev]"
      - run: ./scripts/check_repo.sh
      - run: TRACE_RUNS_DIR="${RUNNER_TEMP}/trace-demo" ./scripts/demo.sh

  dashboard:
    name: Dashboard gate
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: apps/dashboard
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm
          cache-dependency-path: apps/dashboard/package-lock.json
      - run: npm ci
      - run: npm run format:check
      - run: npm run lint
      - run: npm run typecheck
      - run: npm run test:run
      - run: npm run build
```

- [ ] **Step 2: Verify locally**

Run the backend and dashboard command sequences exactly as CI will run them.

Expected: all exit 0.

### Task 3: Acceptance record and publication

**Files:**
- Create: `docs/acceptance/2026-07-28-clean-checkout.md`
- Include: the approved design and all three plan documents in this branch.

**Interfaces:**
- Consumes: the already completed fresh-clone run and this branch's verification.
- Produces: durable evidence for TRA-78 and TRA-52.

- [ ] **Step 1: Write the acceptance record**

Include reviewed commit, install commands, 379 backend tests, smoke/demo results, 30 dashboard tests, build result, and the dependency-audit boundary.

- [ ] **Step 2: Run the full gate again**

Run backend and dashboard gates on the exact commit to publish.

- [ ] **Step 3: Commit and publish**

Stage only this plan's files, commit, push, and open a pull request against `main`.

- [ ] **Step 4: Verify GitHub Actions**

Wait for both `Backend gate` and `Dashboard gate` to succeed. Fix only evidence-backed failures.

- [ ] **Step 5: Merge and enforce**

Merge the pull request, then configure `main` to require both successful checks if repository permissions permit. Read back the protection configuration.
