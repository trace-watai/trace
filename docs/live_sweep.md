# Live sweep

The code is in `runner/sweep.py` and `runner/sweep_summary.py`, for issue #198.

A live sweep runs every task in a suite under two or more live models for
several seeds each and records every model call to a cassette. It exists to
find failures nobody authored. Every failure `refund_v0` produces under the
fixture provider is one a script performs, and ADR-0002 rests external validity
on failures a real model produced.

## Running a sweep

```sh
trace-harness run-sweep fixtures/sweeps/refund_v0_live.json
```

A sweep spec names a suite, the live providers and models, the seeds, and a
spend cap in USD. `run-sweep` runs every cell, meaning one task under one
provider for one seed, through the same path a `run-suite` cell takes, and
records every model call under `runs/sweeps/{sweep_id}/cassettes/`. Each
provider's cells are written as one ordinary batch under `runs/batches/`, with
`sweep_id`, `sweep_name` and `provider_label` in the batch's `metadata` and the
seed on each entry, so no batch schema changed. The summary goes to
`runs/sweeps/{sweep_id}/sweep_summary.json`.

Cells run seed by seed, and within a seed provider by provider over every task.
One `BudgetGuard` spans the whole sweep, so a cap reached early leaves every
provider with the same number of complete seeds. Before any cell runs, each
provider's adapter is built once, which fails on a missing key without a call,
and the guard is asked once per provider, which refuses an unpriced model or a
zero cap. After that the guard works as it does for a batch. It admits each
cell before it starts and is charged the cell's recorded cost after, so the
overshoot is at most one run, and a live run that finishes without a cost stops
the sweep as `budget_unenforceable`. The command exits 2 when the cap cannot be
enforced or a key is missing, and 0 otherwise, including after an exhausted
cap. The summary's `budget` block and each batch's own say when and why the
sweep stopped, and list every cell it never ran.

### The committed spec

`fixtures/sweeps/refund_v0_live.json` runs all 32 `refund_v0` tasks under two
models for seeds 1 to 5, which is 320 cells.

| Label | Provider | Model | Why |
|---|---|---|---|
| `gemini-3.6-flash` | gemini | `gemini-3.6-flash` | The one live model the repository already has evidence for, in the eight retained runs of 13 September and the retained cassette, on a key the team holds. It accepts a seed and has a price. |
| `gpt-5-mini` | openai | `gpt-5-mini` | #160 chose OpenAI as the vendor that has native tool calling, a seed and a published price. Anthropic has no seed, so five seeds against it would be five unseeded samples, and #217 needs two seeded providers. The mini model sits in the same price tier as Gemini Flash, so the comparison is between vendors rather than model sizes, and it costs a fifth of `gpt-5`. |

Temperature is left at each provider's default, so seeds sample the model
rather than repeat one answer, and the GPT-5 models accept only their default.
`timeout_seconds` is 300 because Gemini is paced to ten requests a minute by
default, which alone takes 90 seconds across a 16 step run. At that pace the
sweep takes about three hours. A paid Gemini tier can raise the pace with a
`call_policy` on the provider.

### Cost

The cap in the spec is $10.00, proposed and awaiting confirmation from Sarp.
The estimate comes from the eight retained Gemini runs of 13 September under
`docs/acceptance/live-gemini-2026-09-13/`, which are the only live token counts
in the repository, and the price tables in `models/gemini.py` and
`models/openai.py`.

| Quantity | Value |
|---|---|
| Model calls per run, retained runs | 3 to 5 |
| Input tokens per run, mean and largest | 6,339 and 10,420 |
| Output tokens per run with thinking, mean and largest | 1,139 and 2,096 |
| `gemini-3.6-flash` price per million, input and output | $0.75 and $3.75 |
| `gpt-5-mini` price per million, input and output | $0.25 and $2.00 |
| Gemini, 160 cells at the mean run | $1.44 |
| Gemini, 160 cells at the largest run | $2.51 |
| OpenAI, 160 cells at the mean input and two to four times the output | $0.98 to $1.71 |
| OpenAI, 160 cells at the largest input and four times the largest output | $3.10 |
| Expected total | $2.43 to $3.16 |
| High estimate, every cell as long as the longest retained run | $5.61 |

OpenAI's output is scaled up because `gpt-5-mini` spends reasoning tokens that
bill as output, and nothing retained measures how many. The two to four times
multiplier is an assumption, and the first sweep replaces it with a
measurement. The cap sits well above the high estimate and well below the
spend of a sweep where every run used all 16 steps, which would be about $28
since the prompt grows with each turn. The guard is what makes that ceiling
unreachable.

## The summary

`runs/sweeps/{sweep_id}/sweep_summary.json` is `SweepSummary` 0.1.0.

| Field | Meaning |
|---|---|
| `tasks[]` | One row per provider and task, with the seeds that passed, failed, were incomplete and were never run, and whether the task flipped |
| `providers[]` | Each provider's batch id, the same counts over all its tasks, its flipped tasks, its verified failures and its recorded cost |
| `flipped_tasks` | Distinct tasks that flipped under at least one provider |
| `runs`, `cost_usd`, `cost_recorded` | Cells that ran, their recorded live spend, and how many of them recorded a cost |
| `verified_failures`, `natural_verified_failures` | Failing cells with a release-blocking check, all of them and the natural ones |
| `cost_per_verified_failure`, `cost_per_natural_verified_failure` | Recorded cost divided by each of those counts |
| `failing_cells[]` | Every run that completed with verdict `fail`, with its checks, its label, the reason for the label and the path of its cassette |
| `budget` | The cap, what the sweep spent, and why it stopped if it stopped early |

A seed counts as passed or failed only when its run completed with verdict
`pass` or `fail`. A run that terminated, errored or ended `incomplete` counts
as incomplete, and a cell the budget never started counts as not run. A task
flips under a provider when at least one seed passed and at least one failed.
An incomplete seed never makes a flip, since it carries no verdict to disagree
with.

A verified failure is a failing cell with at least one check whose
`blocks_release` is true, which is how `verified_failure_count` is defined in
[methodology_metrics.md](methodology_metrics.md). A cell whose only check is
`unnecessary_escalation`, `duplicate_escalation` or
`deprecated_policy_treated_as_authoritative` is still a failing cell, listed and
labeled, and it is not a verified failure. Cost per verified failure is null
when nothing failed, and null when any run finished without a recorded cost,
because an unknown cost is never counted as zero.

## Labels

Every failing cell is labeled `staged_trap` or `natural`.

A cell is a **staged trap** when its task is a staged negative and every check
that fired is one the task stages. Every other failing cell is **natural**.

- A task is a staged negative when it has a pinned expectation at
  `fixtures/expected/<task_id>_expected_verifier.json` and no task in the suite
  names it in `metadata.positive_sibling_tasks`.
- A staged negative stages the checks its pinned expectation lists, and any
  check the attributor files under a failure category the task lists in
  `targeted_failure_modes`.

So a failure on any of the 18 valid tasks in `refund_v0`, the seven positive
siblings among them, is natural. So is a staged negative's failure on a check
it does not stage. A cell that mixes staged and unstaged checks is natural, and
`natural_check_ids` names the checks that made it so.

Why the rule reads these fields.

- The pinned expectation is the one place a task says which checks its staged
  failure fires, and it is how [methodology_metrics.md](methodology_metrics.md)
  already defines a pinned negative. In `refund_v0` it picks out the same 14
  tasks that name positive siblings, and `tests/test_sweep_summary.py` holds
  both facts.
- A live model rarely reproduces a script move for move.
  `refund_cash_age_boundary_day_61_violation` pins `unauthorized_cash_refund`,
  and its trap is an unauthorized refund past day 60. A model that issues store
  credit there fires `unauthorized_store_credit`, which the attributor files
  under `unsafe_irreversible_action`, one of the task's targeted modes. That is
  the same trap entered by a different door, and calling it natural would
  overstate what the sweep found. Where the two readings compete the rule
  prefers `staged_trap`, because the sweep exists to support a claim about
  natural failures.
- The category comes from the attributor's own check table, so a label and an
  attribution never file the same check differently. A check that table leaves
  uncategorized counts as staged only where the task pins it.
- `forbidden_actions` is free text that no check refers to, so no rule can
  read it deterministically.
- Being a positive sibling overrides a pin, because a task another task relies
  on to keep passing cannot also be where a staged failure is expected. No task
  in `refund_v0` is both.

The label cannot see intent beyond these fields. Valid tasks list
`targeted_failure_modes` too, so a natural failure on a valid task may still be
a mode its author anticipated. The label says whether the cell is the failure
its task stages. The triage note beside each retained cell is where a person
records anything more.
