# Task-validity rubric — what makes a task good to evaluate on

A task can be *well-formed* (parses against `TaskSpec`) yet still be a *bad* test
of an agent. This rubric is the standard for the second bar. It has two layers:

1. **Structural** — enforced by `TaskSpec` (`trace_harness/tasks/schemas.py`).
   Every task, including unit-test stubs, must satisfy these or it won't load.
2. **Authoring quality** — enforced by `tasks/validation.py` (`validate_task`)
   and run against real fixtures via `python -m trace_harness.tasks.validation`.
   Stubs are exempt; only committed fixtures must pass.

Owner: Emily Au (task design). Failure-mode vocabulary is co-owned with Darrel
(attribution); check ids / verifier semantics with Karan.

## Layer 1 — Structural (the schema enforces these)
- **Unknown fields rejected** (`extra="forbid"`) — typos fail at load time.
- **`task_id`** is a lowercase slug; **`workflow_type`** is dotted-lowercase
  (e.g. `support.refund`); **`schema_version`** is semver.
- **`title` / `description` / `goal`** are non-empty.
- **List fields** (`available_tools`, `available_docs`, `verifier_ids`,
  `targeted_failure_modes`) contain no duplicates.
- **Docs resolvable** — if `available_docs` is set, a `docs_fixture` (or inline
  `initial_state["docs"]`) must provide them.

## Layer 2 — Authoring quality (validate-fixtures enforces these)
Errors block a seed task; warnings should be reviewed.

| Rule (code) | Severity | What it requires / why |
|---|---|---|
| `empty_available_tools` | error | The agent needs something to act with. |
| `empty_verifier_ids` | error | Without a verifier nothing can decide pass/fail. |
| `empty_targeted_failure_modes` | error | A task must target a known failure to be useful for attribution. |
| `no_correct_behavior` | error | `expected_behavior` and/or `forbidden_actions` must define what correct looks like. |
| `clock_in_initial_state` | error | No wall-clock time in state — encode time as relative ages (e.g. `purchase_age_days`) so runs are reproducible. |
| `vague_language` | warning | `goal`/`description` should state a concrete outcome, not "do right by the customer". |
| `not_multi_step` | warning | Tasks should require multi-step behavior (≥2 tools); single-tool tasks produce thin trajectories. |
| `missing_required_evidence` | warning | `required_evidence` documents what proves pass/fail. |

## Examples
- **Good seed:** `fixtures/tasks/refund_policy_valid_cash.json`,
  `fixtures/tasks/refund_policy_failure.json` — both pass all rules.
- **Counterexample:** `fixtures/tasks/counterexamples/refund_policy_ambiguous.json`
  — vague goal, no verifier, no failure modes, no defined behavior. Counterexamples
  live in the `counterexamples/` subfolder (not globbed as seed tasks); the
  validate-fixtures runner expects everything there to be flagged.

## Running it
```bash
python -m trace_harness.tasks.validation        # validate every committed task fixture
python -m pytest tests/test_task_validation.py  # the checker's tests
```

## Adding a new task — quick checklist
Concrete `goal`; complete deterministic `initial_state` (no clocks); the tools it
needs in `available_tools`; `expected_behavior` and/or `forbidden_actions`;
at least one `verifier_id` and one `targeted_failure_mode`; `required_evidence`;
a `difficulty`; and a `metadata.fixture_script` if it should run in the pipeline.
Run `python -m trace_harness.tasks.validation` before committing.