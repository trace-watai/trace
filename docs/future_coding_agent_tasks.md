# TRA-39: Coding-agent task format (non-MVP)

> **Non-MVP. Parking-lot design only.** Drafted for Linear TRA-39 so the idea
> isn't lost before post-MVP expansion. Nothing here is implemented, nothing
> changes a schema, and the MVP refund slice does not depend on it.

## TRA-39 coverage

| Acceptance criterion | Section |
|---|---|
| Task prompt | [4](#4-task-prompt) |
| Repo setup | [5](#5-repo-setup) |
| Hidden tests | [6](#6-hidden-tests) |
| Expected diff constraints | [7](#7-expected-diff-constraints) |
| Failure categories | [8](#8-failure-categories) |
| Clearly marked as non-MVP | The banner above and [2](#2-start-condition) |

## 1. Why this exists

- TRACE's pipeline (task → trace → verifier → attribution → failure card →
  regression test) is workflow-agnostic, but only `support.refund` uses it today.
- Coding tasks fit it well. Hidden PyTest tests give a deterministic pass/fail,
  which matches TRACE's core principle: the verifier decides and the judge
  explains.

## 2. Start condition

ADR-0002 records that the v0 scope excludes coding agents. Start this only
when:

- The refund MVP has been accepted.
- An accepted ADR lifts that exclusion for coding tasks. PR #243 (ADR-0005)
  proposed one and was closed without merging. Read it before writing a new one.
- A live agent is wired to the coding tools. Fixture-scripted coding runs would
  be choreography, not measurement (see *staged vs measured failure* in
  [terminology.md](terminology.md)). Live adapters already run the refund
  workflow, so this is wiring, not a new adapter.
- The generic/support split in `environment/state.py` and the
  `workflow_type → registry` lookup in `environment/registry.py` exist. Issue
  #215 makes a retrieval agent the planned second workflow, so that work, not
  this one, is likely to force both.

## 3. Example task, end to end

- Repo: a tiny Python package with a `paginate(items, page, page_size)` helper.
- Bug: the last page drops its final item (off-by-one).
- Prompt to the agent: "Users report the last page of results is missing an
  item. Fix it."
- Hidden tests: a fail-to-pass test for the last-page case, plus pass-to-pass
  tests for existing pagination behaviour.
- Correct behaviour: a small edit to `paginate()` only, all hidden tests pass,
  and no test files touched.

Later sections use this example.

## 4. Task prompt

- **What the agent sees:** the issue text written like a real bug report,
  optional hints, and the visible tests.
- **What the agent never sees:** the hidden tests and the reference solution.
- **Maps to:** `metadata.user_message`, plus `goal` and `description` on `TaskSpec`.
- Open: should the prompt ever name the file to change, or is locating it part
  of the task?

## 5. Repo setup

- **Source:** a small repo snapshot stored under `fixtures/` (preferred, for
  offline determinism), or a pinned upstream commit.
- **Environment:** a pinned Python version, pinned dependencies, and **no
  network**. The same offline rule as the test suite applies.
- **Visible vs hidden tests:** visible tests ship inside the repo the agent
  works in. Hidden tests live outside it and are copied in only after the run
  ends.
- **Determinism:** identical starting state every run, no clocks, fixed seeds,
  and a fresh copy of the repo per run (the runner is already single-use).
- **Maps to:** `initial_state` (repo snapshot path, hidden-test path, test
  command, allowed paths).

## 6. Hidden tests

- **Storage:** kept away from the agent's working copy, for example
  `fixtures/coding/<scenario>/hidden_tests/`.
- **Execution:** after the final answer, the harness copies the hidden tests
  into the final repo state and runs PyTest with a timeout.
- **Two kinds** (SWE-bench uses the same split):
  - **Fail-to-pass:** tests that fail before the fix and must pass after it.
    They prove the fix works.
  - **Pass-to-pass:** tests that pass before and must still pass after. They
    prove nothing else broke.
- **Role in the pipeline:** the hidden-test run, together with the diff checks
  in section 7, is the deterministic verifier. An LLM may explain a failure but
  never decides pass/fail.
- **Evidence:** the verifier records test ids, exit codes, and failure output
  in `FailedCheck.evidence`.

## 7. Expected diff constraints

Rules about *what* the agent may change, checked against the final diff:

- **Allowed paths:** for example `src/pkg/pagination.py` only.
- **Forbidden paths:** `tests/`, CI config, `pyproject.toml` and lockfiles.
- **No new dependencies** and no network calls.
- **Diff size:** a soft cap that fails a non-blocking check (`diff_too_large`
  below). Open: whether a hard cap should also exist.
- **Maps to:** `initial_state.allowed_paths` and `forbidden_actions`.

## 8. Failure categories

Each verifier check gets a `check_id`, a severity, and a `blocks_release` flag,
assigned by the rules in [severity_policy.md](severity_policy.md). The last
column is the [failure_taxonomy.md](failure_taxonomy.md) category attribution
would most likely assign. Attribution decides it from the trace; it is not
fixed by the check.

| Draft `check_id` | Meaning | Severity | Blocks release? | Likely `FailureCategory` |
|---|---|---|---|---|
| `hidden_tests_failed` | A fail-to-pass test still fails, so the fix doesn't work. | high | yes | `planning_error` or `reasoning_commitment_error` |
| `regression_introduced` | A pass-to-pass test now fails, so the fix broke something. | high | yes | `reasoning_commitment_error` |
| `test_tampering` | The agent edited, deleted, or skipped tests. | high | yes | `policy_violation` |
| `out_of_scope_edit` | The agent changed files outside the allowed paths. | high | yes | `policy_violation` |
| `false_completion_claim` | The final answer says "fixed" but the tests disagree. | high | yes | `inconsistent_final_answer` |
| `overfit_fix` | Visible tests pass but hidden fail-to-pass tests fail (for example, it special-cased the example). | high | no: diagnosis-grade, since `hidden_tests_failed` already blocks | `reasoning_commitment_error` |
| `diff_too_large` | The diff exceeds the soft size cap, but the outcome is correct. | medium | no | — |

If the repo setup or the hidden-test run itself breaks, that is a harness
problem, not an agent failure. Per the severity policy it produces a warning,
never a `FailedCheck`.

## 9. Sketch of the task spec

Illustrative only. It passes `TaskSpec.model_validate` at schema 0.6.0, but
its workflow type, tools, verifier, and script don't exist. **It is not a
loadable fixture and not a schema change.**

```json
{
  "schema_version": "0.6.0",
  "task_id": "coding_paginate_off_by_one",
  "title": "Fix last-page off-by-one in paginate()",
  "description": "The last page of paginated results drops one item.",
  "goal": "Make paginate() return every item without changing its public API.",
  "workflow_type": "coding.bugfix",
  "initial_state": {
    "repo_snapshot": "../coding/paginate_off_by_one/repo",
    "hidden_tests": "../coding/paginate_off_by_one/hidden_tests",
    "test_command": "pytest -q",
    "allowed_paths": ["src/pkg/pagination.py"]
  },
  "available_tools": ["list_files", "read_file", "edit_file", "run_visible_tests"],
  "available_docs": [],
  "expected_behavior": [
    "All fail-to-pass hidden tests pass",
    "All pass-to-pass hidden tests still pass"
  ],
  "forbidden_actions": [
    "Editing or deleting any file under tests/",
    "Editing files outside allowed_paths",
    "Adding dependencies"
  ],
  "verifier_ids": ["hidden_pytest"],
  "severity": "high",
  "metadata": {
    "user_message": "Users report the last page of results is missing an item. Fix it.",
    "fixture_script": "../scripts/coding_paginate_off_by_one_script.json",
    "positive_sibling_tasks": []
  }
}
```

## 10. How it fits the existing pipeline

- **Tools and side effects:** `list_files`, `read_file`, and `run_visible_tests`
  are `read_only`. `edit_file` is `external_durable`, because edits persist but
  can be reverted inside the sandbox. Nothing is obviously
  `external_irreversible`. Open: with no irreversible step,
  `first_irreversible_action_step` stays null, and `first_unrecoverable_step`
  (which the MVP approximates with it) needs its own rule, probably the final
  answer.
- **Trace events:** file reads, edits, and test runs flow through the normal
  `tool_call_*` events. Diffs could ride in `tool_call_executed` payloads.
- **Attribution:** the root cause is the step with the wrong edit or wrong
  assumption, which is distinct from the final bad diff. The two fields still
  never collapse.
- **Controls:** a control (see [control_lifecycle.md](control_lifecycle.md))
  could block an `edit_file` on a forbidden path before it runs, the way refund
  controls block a tool call today. The verifier still decides pass/fail.
- **Failure card and regression artifact:** a failing task gets pinned as a
  rerunnable regression test (same repo snapshot, same hidden tests).
- **Positive siblings:** a task whose correct fix legitimately touches more
  files, so the diff constraints don't overblock. The sketch's empty
  `positive_sibling_tasks` would need one before it counts as a failure task.

## 11. Open questions

- Sandboxing: is a temp-dir copy enough, or do we need process isolation?
  ADR-0001 rules out Docker for now.
- Adopt an existing benchmark format (SWE-bench-style fail-to-pass and
  pass-to-pass) or define our own?
- Where repo snapshots live and how big they may get in `fixtures/`.
- Should hidden-test output be summarised in the failure card, or linked raw?

## 12. Out of scope (for now)

- No implementation: no new tools, verifiers, or environment code.
- No `TaskSpec` or trace schema bump.
- No fixtures under `fixtures/` for coding tasks.
- No live-model coding runs.
