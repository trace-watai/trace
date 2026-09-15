# Suite report (`suite_report.json`)

A **batch summary** (`runs/batches/{batch_id}/batch_summary.json`) says how
many runs passed and failed. It does not say *which* verifier checks fired,
which failure categories the failures fell into, or which of the failure
modes the tasks claim to target never actually showed up. All of that is
already on disk after a batch. The **suite report** reads it back.

- **Model:** `SuiteReport` in `src/trace_harness/runner/report.py`, schema
  `0.1.0`.
- **Written to:** `runs/batches/{batch_id}/suite_report.json`, with a
  markdown rendering at `suite_report.md` beside it.
- **Produced by:** `trace-harness run-suite <manifest> --report` (at the end
  of the batch) or `trace-harness report-suite <batch_id>` (any time after).
  Both call `build_suite_report(summary, store)` — a **read-only** roll-up
  that never re-runs a task and never touches `batch_summary.json`.
- **Read by:** `RunReader.get_suite_report(batch_id)` (builds it in memory
  if not yet persisted). A TypeScript mirror for the dashboard is a later
  ticket, in the style of `apps/dashboard/src/data/run-loader.ts`.

Per run, the builder reads four artifacts through `ArtifactStore.read_json`:
`task_spec.json`, `verifier_result.json`, `attribution_result.json`,
`regression_artifact.json`. A missing downstream artifact is tolerated — see
[Degradation](#degradation).

## Top-level fields

| field | type | derivation |
| --- | --- | --- |
| `schema_version` | str | `"0.1.0"` (`SUITE_REPORT_SCHEMA_VERSION`). |
| `batch_id` | str | `BatchSummary.batch_id`. |
| `suite_id` | str | `BatchSummary.suite_id`. |
| `generated_at` | datetime | wall clock when the report was built (`utc_now()`). |
| `total_rows` | int | `len(rows)` = number of batch cells (tasks × agent configs). |
| `failing_rows` | int | rows with `verifier_passed is False`. |
| `rows` | list | one `SuiteReportRow` per `BatchSummary.entries` item, same order. |
| `totals` | object | roll-ups over `rows` — see [Totals](#totals). |
| `coverage` | object | claimed vs. observed failure modes — see [Coverage](#coverage). |
| `warnings` | list[str] | non-fatal problems hit while building (missing artifacts). |

## Row fields (`SuiteReportRow`)

| field | type | derivation |
| --- | --- | --- |
| `run_id` | str \| null | `BatchRunEntry.run_id`; `null` only when the cell's setup failed before a run existed. |
| `task_id` | str | `BatchRunEntry.task_id`. |
| `family` | str | first path segment after `refund_task_families/` in `BatchRunEntry.task_path`; `canonical` for tasks outside that folder. |
| `agent_label` | str | `BatchRunEntry.agent_label`. |
| `verifier_passed` | bool \| null | `BatchRunEntry.verifier_passed` (`null` = verify never ran, e.g. setup error). |
| `failed_check_ids` | list[str] | sorted `check_id`s from `verifier_result.json` `failed_checks`; `[]` on a pass. |
| `severity` | str \| null | `verifier_result.json` `severity` (falls back to the entry's), `null` on a pass. |
| `blocks_release` | bool | `verifier_result.json` `blocks_release`; `false` on a pass. |
| `primary_failure_category` | str | `attribution_result.json` `primary_failure_category`. `""` for a pass / un-verified row; `"unknown"` for a failure whose attribution file is missing **or** whose check has no attributor category mapping. |
| `contributing_failure_categories` | list[str] | `attribution_result.json` `contributing_failure_categories`; `[]` otherwise. |
| `root_cause_step` | int \| null | `attribution_result.json` `root_cause_step`. |
| `first_irreversible_action_step` | int \| null | `attribution_result.json` `first_irreversible_action_step`. |
| `positive_sibling_task_ids` | list[str] | file stems of `task_spec.json` `metadata.positive_sibling_tasks[].task_fixture` (the sibling task ids that must keep passing). |
| `regression_test_name` | str \| null | `regression_artifact.json` `test_name`; `null` when the run was not bundled. |

## Totals

Every dict is key-sorted so the JSON diffs cleanly.

| field | derivation |
| --- | --- |
| `by_check_id` | for each `check_id`, the number of **failing rows** it fired on. A compound failure counts once per distinct check. |
| `by_failure_category` | for each category, the number of **failing rows** whose `primary_failure_category` is that value. Sums to `failing_rows`. Includes `unknown`. |
| `by_family` | number of rows (pass **and** fail) in each family. Sums to `total_rows`. |
| `by_agent_label` | number of rows per agent label. Sums to `total_rows`. |
| `pass_rate_by_family` | `passes / rows-with-a-verdict` per family, 4-dp. Rows with `verifier_passed is null` (setup errors) are excluded. |

### Consistency with the failure-bundle doc

For `refund_v0`, `by_failure_category` is
`{clarification_failure: 1, inconsistent_final_answer: 2, stale_source_authority: 1, unknown: 5, unsafe_irreversible_action: 2}`.
The non-`unknown` entries line up with
[`failure-bundles-v0.md`](acceptance/failure-bundles-v0.md): bundle #1
(`refund_policy_failure`) → `stale_source_authority`; the authorization-bypass
negatives → `unsafe_irreversible_action`; the missing-escalation negative →
`clarification_failure`; the phantom-refund negative →
`inconsistent_final_answer`. The five `unknown` rows are the escalation-hygiene
(`unnecessary_escalation`, `duplicate_escalation`) and retrieval-completeness
(`policy_not_retrieved_before_action`, `incomplete_retrieval_coverage`) checks,
which have no entry in the heuristic attributor's check→category map yet.

## Coverage

The **claimed** side is the union of every row's `task_spec.json`
`targeted_failure_modes` (free strings). The **observed** side is the union
of `primary_failure_category` and `contributing_failure_categories` over
**failing rows only**; the `unknown` sentinel is never counted as observed.

| field | derivation |
| --- | --- |
| `claimed_vs_observed` | `{claimed mode → sorted categories observed on failing runs of tasks that declared that mode}`. An empty list = the mode is claimed but its failing tasks produced no (non-`unknown`) category. A non-empty list whose entries differ from the key is normal — the task failed a different way than it advertised. |
| `claimed_never_observed` | `sorted(claimed − observed)` — modes no failing run anywhere produced as a category. Non-empty for `refund_v0` (proves the coverage logic ran). |
| `observed_never_claimed` | `sorted(observed − claimed)` — categories that showed up but no task listed as a target. `unknown` is excluded by construction. |

### `refund_v0` coverage gap (current)

`claimed_never_observed`:
`grounding_citation_error`, `overblocking`, `policy_violation`,
`premature_termination`, `query_formation_error`, `retrieval_selection_error`,
`state_tracking_error`, `tool_selection_error`, `unnecessary_escalation`,
`unproductive_loop`.

`observed_never_claimed`: _(none)_.

Most of that gap is **positive-control tasks** (a `policy_violation` task that
correctly *doesn't* violate produces no category) plus the **attributor
mapping gap** above — retrieval-completeness tasks claim
`query_formation_error` / `grounding_citation_error` / `retrieval_selection_error`
but their checks resolve to `unknown`. Closing the map (attribution ticket) or
adding tasks that actually exercise a mode both shrink this list; it is the
"issue #13 coverage matrix" kept honest by regeneration instead of by hand.

## Degradation

`build_suite_report` never raises on missing data:

- **Failing run, no `attribution_result.json`** → `primary_failure_category`
  is `"unknown"`, a warning is appended
  (`"<task_id>: verifier failed but attribution_result.json is missing; …"`).
- **No `task_spec.json`** → `targeted_failure_modes` and siblings are omitted
  for that row, with a warning.
- **No `verifier_result.json`** → check ids empty, severity/blocks fall back
  to the batch entry.
- **No `regression_artifact.json`** (run not bundled) → `regression_test_name`
  is `null`, silently.
- **Setup-error entry** (`run_id is null`) → still a row: `family` from the
  path, `verifier_passed` `null`, all failure fields inert.

## The pinned report

`fixtures/expected/refund_v0_suite_report.json` is a full `refund_v0` report
with the volatile fields removed (`batch_id`, `generated_at`, and every row's
`run_id` set to `null`). `tests/test_suite_report.py` runs `refund_v0` into a
temp dir, applies the same normalization, and asserts equality — 29 rows, 11
failing. Regenerate it only by re-running `build_suite_report` (never hand-edit).

> Note on `refund_bundles_v0`: "five different categories across the five
> failing rows" refers to five distinct **bundles** (each tripping a
> different verifier check, per `failure-bundles-v0.md`), not five mutually
> unique category *strings* — confirmed with the TPM. The attributor maps
> both the day-31 and day-45 authorization bypasses to
> `unsafe_irreversible_action`, so the report's primary-category set has
> **four** distinct values over those five rows; that's expected, since the
> two bundles share a category by design while remaining distinct cases.
> `tests/test_suite_report.py` asserts exactly that: five failing rows, one
> per bundle, every one categorized (no `unknown`) — a real contrast with
> `refund_v0`, where 5 of 11 failing rows are `unknown`.
