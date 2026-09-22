# Task-validity rubric — what makes a task good to evaluate on

A task can be *well-formed* (parses against `TaskSpec`) yet still be a *bad* test
of an agent. This rubric is the standard for the second bar. It has two layers:

1. **Structural** — enforced by `TaskSpec` (`trace_harness/tasks/schemas.py`).
   Every task, including unit-test stubs, must satisfy these or it won't load.
2. **Authoring quality** — enforced by `tasks/validation.py` (`validate_task`)
   and run against real fixtures via `trace-harness validate-fixtures`.
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
- **Escalation posture**: a `conditional` posture in `expected_action.escalation`
  must name its `condition`, and a `required` or `forbidden` posture may carry
  neither a `condition` nor `claim_made`.

## Layer 2 — Authoring quality (validate-fixtures enforces these)
Errors block a seed task; warnings should be reviewed.

| Rule (code) | Severity | What it requires / why |
|---|---|---|
| `empty_available_tools` | error | The agent needs something to act with. |
| `empty_verifier_ids` | error | Without a verifier nothing can decide pass/fail. |
| `empty_targeted_failure_modes` | error | A task must target a known failure to be useful for attribution. |
| `unknown_failure_mode` | warning | `targeted_failure_modes` should draw from the `FailureCategory` taxonomy (attribution, Darrel) so labels join with the Judge's `failure_category`. Non-taxonomy labels (e.g. positive-control guards like `overblocking`) warn — confirm them with Darrel. |
| `no_correct_behavior` | error | `expected_behavior` and/or `forbidden_actions` must define what correct looks like. |
| `clock_in_initial_state` | error | No wall-clock time in state — encode time as relative ages (e.g. `purchase_age_days`) so runs are reproducible. |
| `vague_language` | warning | `goal`/`description` should state a concrete outcome, not "do right by the customer". |
| `not_multi_step` | warning | Tasks should require multi-step behavior (≥2 tools); single-tool tasks produce thin trajectories. |
| `missing_required_evidence` | warning | `required_evidence` documents what proves pass/fail. |
| `requires_escalation_without_tool` | error | A task with `requires_escalation: true` must offer the `escalate_case` tool in `available_tools`, or a correct run could not escalate. (Plain string check — independent of whether the tool has landed in the environment yet.) |
| `conditional_escalation_claim_undeclared` | error | A `conditional` escalation posture must declare `claim_made`, which records whether the customer makes the claim its `condition` names. Left undeclared, the verifier infers the claim by matching `metadata.user_message`, and that matching misreads requests, questions and negations (TRA-79). Tasks and run artifacts from before task schema 0.6.0 still load and take that fallback. |

## Examples
- **Good seed:** `fixtures/tasks/refund_policy_valid_cash.json`,
  `fixtures/tasks/refund_policy_failure.json` — both pass all rules.
- **Counterexample:** `fixtures/tasks/counterexamples/refund_policy_ambiguous.json`
  — vague goal, no verifier, no failure modes, no defined behavior. Counterexamples
  live in the `counterexamples/` subfolder (not globbed as seed tasks); the
  validate-fixtures runner expects everything there to be flagged.

## Running it
```bash
trace-harness validate-fixtures        # validate every committed task fixture
python -m pytest tests/test_task_validation.py  # the checker's tests
```

## Adding a new task — quick checklist
Concrete `goal`; complete deterministic `initial_state` (no clocks); the tools it
needs in `available_tools`; `expected_behavior` and/or `forbidden_actions`;
at least one `verifier_id` and one `targeted_failure_mode`; `required_evidence`;
a `difficulty`; and a `metadata.fixture_script` if it should run in the pipeline.
If a correct run must escalate to a human rather than resolve the case, set
`requires_escalation: true` and include `escalate_case` in `available_tools`.
If escalating is correct only because the customer makes a claim the order
record cannot confirm, set `expected_action.escalation` to a `conditional`
posture with its `condition` and `claim_made: true`, or `claim_made: false`
when the customer makes no such claim.
Run `trace-harness validate-fixtures` before committing.