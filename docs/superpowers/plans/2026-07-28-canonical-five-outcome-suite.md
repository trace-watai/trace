# Canonical Five-Outcome Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the committed canonical suite execute all five already-runnable MVP refund outcomes with pinned aggregate expectations.

**Architecture:** Keep the existing explicit-path `SuiteSpec`; expand only its manifest. A real BatchRunner test executes the manifest and asserts exact task/verdict/status coverage so specification-only files cannot inflate the result.

**Tech Stack:** JSON fixtures, Pydantic suite models, pytest, TRACE BatchRunner.

## Global Constraints

- Do not include specification-only task-family files.
- Preserve one intentional verifier failure and four correct passes.
- Do not describe broader variant-family coverage as complete.

---

### Task 1: Pin the five-outcome contract with a failing test

**Files:**
- Modify: `tests/test_suite.py`
- Modify later: `fixtures/suites/refund_v0.json`

**Interfaces:**
- Consumes: `load_suite`, `BatchRunner`, and the five top-level canonical tasks.
- Produces: an executable manifest acceptance test.

- [ ] **Step 1: Expand the manifest-loading assertion**

Change `test_load_sample_suite` to expect this ordered membership:

```python
[
    "fixtures/tasks/refund_policy_failure.json",
    "fixtures/tasks/refund_policy_valid_cash.json",
    "fixtures/tasks/refund_policy_store_credit.json",
    "fixtures/tasks/refund_policy_no_refund.json",
    "fixtures/tasks/refund_policy_missing_info.json",
]
```

- [ ] **Step 2: Add the executable outcome test**

Add a test that loads `SUITE_MANIFEST`, executes `BatchRunner`, and asserts:

```python
{
    "refund_policy_failure": False,
    "refund_policy_valid_cash": True,
    "refund_policy_store_credit": True,
    "refund_policy_no_refund": True,
    "refund_policy_missing_info": True,
}
```

Also assert total 5, completed 5, terminated 0, errored 0, passed 4, failed 1.

- [ ] **Step 3: Verify RED**

Run: `.venv/bin/pytest tests/test_suite.py -q`

Expected: failure because the manifest still contains two tasks.

### Task 2: Expand the manifest and document coverage

**Files:**
- Modify: `fixtures/suites/refund_v0.json`
- Create: `docs/acceptance/refund-v0-suite.md`

**Interfaces:**
- Consumes: the five existing task/script pairs.
- Produces: one reproducible five-outcome suite and coverage table.

- [ ] **Step 1: Add the remaining three task paths**

Keep one fixture agent configuration and add store credit, no-refund, and missing-information tasks.

- [ ] **Step 2: Verify GREEN**

Run: `.venv/bin/pytest tests/test_suite.py -q`

Expected: all suite tests pass.

- [ ] **Step 3: Add the coverage table**

Document each scenario, expected verdict, expected state/action, verifier behavior, script path, and matched sibling/control. Explicitly list the unfinished broad variant families.

- [ ] **Step 4: Run the repository gate**

Run: `PYTHONPATH=src PATH=.venv/bin:$PATH ./scripts/check_repo.sh`

Expected: all checks pass.

- [ ] **Step 5: Commit, publish, and merge**

Commit only suite/test/coverage files, open a PR, require both integration checks, merge, and read back `main`.
