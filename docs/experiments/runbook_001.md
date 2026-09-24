# Runbook 001. Running exp_001

Issue #200, Linear TRA-122. [Pre-registration 001](preregistration/001.md)
binds every step here. Where issue #200 says something else, the
pre-registration wins, and the last section lists each difference. Reading the
numbers is #201.

The plan is
`docs/acceptance/experiments/exp_001_replay_validity/experiment.json`. It was
written before any condition ran and is committed unfrozen. It is frozen in
step 2, right before the first batch, and git history must show no change to it
after that commit.

## Before freeze

`metadata.needs_confirmation` in the plan lists four values that wait on Sarp.

| Value | In the plan | Basis |
|---|---|---|
| Cap | `budget.max_cost_usd` 50.0 | The pre-registration caps recorded `cost_usd` across all live arms at 50 US dollars |
| Temperature | `null` in every live agent config, so none is sent and each `run_config.json` records null | "at the provider's default temperature" |
| Live model | gemini `gemini-3.6-flash` for `live` and `live_no_control` | "Gemini (`gemini-3.6-flash`, the adapter default)" |
| Swapped model | anthropic `claude-sonnet-5` for `live_swapped` | The pre-registration names #160's second adapter and no model, and this is that adapter's default |

Three more things hold before freeze.

- #160 is on main before the first live run. Otherwise `live_swapped` drops
  out of step 4 and is reported as not run, with no substitute model.
- The #198 slot in `metadata.amendment_slots` is filled or stays empty. A
  natural failing cell joins only through a dated amendment appended to the
  pre-registration, with its fork point and four conditions added to the plan
  in the same commit, and only when its materialized `replay_mode_basis`
  records a block under `ctl_refund_window_v1`. A cell from the canonical or
  purchase_age family adds a pair and no independent family.
- `pytest tests/test_exp_001.py` passes on the tree about to be frozen. While
  the plan is unfrozen it re-materializes each fork point, so a moved fixture
  or verifier shows up here, while the plan can still change.

Confirmed values are recorded in `needs_confirmation` in the commit that
confirms them.

## Commands

From the repository root, with the package installed. Only step 4 calls a
provider. It needs `GEMINI_API_KEY` and `ANTHROPIC_API_KEY` in the
environment, and neither ever enters the repository.

```bash
PLAN=docs/acceptance/experiments/exp_001_replay_validity/experiment.json
FP=docs/acceptance/experiments/exp_001_replay_validity/fork_points
RUNS=runs/exp_001
mkdir -p "$RUNS"
```

**1. Estimate the cost.** It prints each live condition's expected and ceiling
cost and exits 1 if the ceiling passes the cap.

```bash
python scripts/estimate_exp_001_cost.py
```

**2. Freeze the plan and commit it.** The frozen plan is the registration of
this run, and its commit is the last change the plan may show.

```bash
trace-harness experiment freeze "$PLAN"
git add "$PLAN"
git commit -m "Freeze the exp_001 plan before its first batch (#200)"
```

**3. Run the harness check.** The dry run repeats steps 4 to 7 offline on a
copy of the frozen plan with the fixture model in every live arm, in a scratch
folder. It must end with `harness check: PASS`, meaning every pair agrees,
nothing diverges, all eight metrics are non-null and the regeneration matches.
Anything else is a harness defect, and the pre-registration stops the run
there. Since no batch has run yet, fixing it means reverting the freeze commit
and freezing again after the fix.

```bash
python scripts/dry_run_exp_001.py
```

**4. Branch every condition,** one arm at a time across the three fork points.

```bash
for arm in static_replay live live_no_control live_swapped; do
  for fork in \
      refund_policy_failure:run_20260924T010656Z_609a294e \
      refund_cash_age_boundary_day_31_no_approval:run_20260924T010657Z_7c9d18f3 \
      refund_cash_age_boundary_day_61_violation:run_20260924T010658Z_f24f3b5c; do
    trace-harness --runs-dir "$RUNS" branch "$FP/${fork#*:}/regression_artifact.json" \
      --experiment "$PLAN" --condition "${arm}__${fork%%:*}" | tee -a "$RUNS/branch.log"
  done
done
```

- Each invocation checks the frozen set before any run and exits 2 on drift.
- The cap spans every invocation into `$RUNS`, and `branch` prints what the
  earlier batches spent. The swapped arm runs last, so a cap reached late costs
  it first.
- A seed that ends incomplete is replaced from seeds 5 to 9 inside the same
  invocation.
- Live runs record cassettes under the experiment folder, one folder per arm.
  Recording refuses to overwrite a cassette, so a condition is branched once. A
  repeated one ends its seeds as `setup_error`, and the first batch is the one
  to record.

**5. Record** every batch, with decision review by human. The pairs come from
the `Record with` lines that step 4 logged.

```bash
trace-harness --runs-dir "$RUNS" experiment record "$PLAN" \
  --decision review --decided-by human \
  $(grep -oE -- '--condition [^ ]+=batch_[^ ]+' "$RUNS/branch.log")
```

It prints the eight metrics, any excluded pair with its reason, and where it
wrote `repair_effectiveness.json`.

**6. Retain** the runs dir and the three recorded files beside the plan. The
script refuses if the recorded plan differs from the retained one, and stops
before copying if a file looks like a provider credential.

```bash
scripts/retain_exp_001.sh "$RUNS"
```

**7. Regenerate** `result.json` and the B1 sidecar offline from what was
retained. Both must print `identical`.

```bash
scripts/regenerate_exp_001.sh
```

**8. Commit** the retained folder, meaning `result.json`, `report.md`,
`repair_effectiveness.json`, `runs/` and `cassettes/`. The plan does not
change, and `git log --oneline -- "$PLAN"` still ends at the freeze commit.

## Cost against the cap

`scripts/estimate_exp_001_cost.py` prices calls through the adapters' own
tables (`GEMINI_PRICING` at 0.75 and 3.75 US dollars per million input and
output tokens, `ANTHROPIC_PRICING` at 3 and 15). Token counts come from the
retained cassette
`fixtures/cassettes/refund_policy_failure/gemini-3.6-flash/default.jsonl`,
whose five calls send 995, 1251, 1750, 2851 and 3573 input tokens and return at
most 690 output tokens. A call at step `s` is priced at the cassette's input for
step `s`, growing by 1101 tokens a step past step 5, and at 690 output tokens.

| Arm | Model | Expected | Ceiling |
|---|---|---|---|
| `live` | gemini-3.6-flash | $0.14 | $3.53 |
| `live_no_control` | gemini-3.6-flash | $0.14 | $3.53 |
| `live_swapped` | claude-sonnet-5 | $0.57 | $14.10 |
| Total | | $0.85 | $21.15 |

Expected assumes five seeds per condition, each making as many calls after the
fork as the recording did. Ceiling assumes ten runs per condition, seeds 0 to 4
and every replacement, each running to the 16 step limit. The Claude line uses
Gemini's token counts, since no Claude run is retained. Pricing every call at
1010 output tokens, the largest single call in the eight retained 2026-09-13
live Gemini runs, gives $1.03 expected and $23.75 at the ceiling. The guard
checks between runs, so the overshoot past the cap is at most one run.

## Stopping rules as the tools apply them

| Pre-registration rule | What happens |
|---|---|
| The harness check fails | Step 3 prints `FAIL` and no live run starts |
| The budget cap is reached | `branch` refuses every later live seed, each batch lists them in `budget.not_run`, and the experiment is recorded as incomplete |
| A frozen path changes | `branch` and `record` exit 2 with the changed files, and a restart needs a new pre-registration |
| A pair ends with fewer than five completed seeds | It is left out of `verdict_agreement_rate`, and `result.metadata.verdict_agreement_pairs`, `report.md` and the `record` output name it with the reason |

## Where issue #200 and the pre-registration differ

- **Fork points.** #200 names the failing refund_v0 runs, the five bundles
  and every natural failing cell from #198. The pre-registration registers
  the 3 failing runs that record a block, which covers 2 of the 5 bundles, and
  the plan has exactly those. The other 15 failing tasks record no block. #198
  cells need the amendment above.
- **Seeds.** #200 asks for at least five seeds in every condition. The
  pre-registration runs `static_replay` once, since it is deterministic.
- **Per-model rates.** The pre-registration rates each live model on its own.
  The headline `verdict_agreement_rate` is the `live` arm's Gemini rate, and
  the swapped model's rate is in `metrics.extra` under
  `verdict_agreement_rate/live_swapped/claude-sonnet-5`.
- **`live_swapped`.** #200 lists it as a condition to run. The
  pre-registration runs it only if #160 is on main before the first live run.
- **Harness check.** #200 does not name it. The pre-registration runs it
  first, and step 3 is it.
- **B1.** #200 asks for B1 per artifact and control. The sidecar has one entry
  per control-on arm, artifact, control and model, and the swapped model's
  entries are null, since the pre-registration gives that model no
  control-off arm.

## What the rehearsal shows about the numbers

- At all three fork points the control acts at the fork step, and B1 counts
  only checks after it. The control-off baseline therefore leaves out the
  refund the recording issues at the fork step. Under the fixture model the two
  purchase_age recordings do nothing blocking after the fork, so their B1 is
  null, and refund_policy_failure keeps failing after the fork either way, so
  its B1 is 0.0. A live model may do otherwise.
- `verified_failure_count` counts every failed run in every batch, static
  replays and the control-off arm included. The rehearsal counts 48.
- `sibling_failure_rate` counts siblings per replay. Three replays run two
  distinct siblings, `refund_policy_valid_cash` twice.

## Known limits

- The fork points were recorded by the fixture adapter and carry no provider
  state, and no test calls a live provider from such a prefix
  ([branch_stage.md](../branch_stage.md#limits)). If a provider rejects the
  prefix, every seed of that condition ends incomplete, the replacement pool
  runs out, and the pair is insufficient.
- The metrics history job scans `docs/acceptance` recursively. Once step 8
  lands, the experiment's batch summaries and repair packages enter its suite
  pass rate and control coverage unless the job excludes
  `docs/acceptance/experiments`.
