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
| Cap | `budget.max_cost_usd` 50.0 | The pre-registration caps recorded `cost_usd` across all live arms at 50 US dollars. The swapped arm records none, so the Gemini arms are what the cap binds |
| Temperature | `null` in every live agent config, so none is sent and each `run_config.json` records null. The swapped arm's outside agent takes none, and Claude Code sends its own request settings | "at the provider's default temperature" |
| Live model | gemini `gemini-3.6-flash` for `live` and `live_no_control` | "Gemini (`gemini-3.6-flash`, the adapter default)" |
| Swapped model | `claude-sonnet-5` through the Claude Code CLI (`trace_harness.agents.claude_code_ref:agent`, `billing: subscription`) for `live_swapped` | The 2026-09-25 amendment to the pre-registration, which replaces #160's API adapter with Claude Code on a Claude plan and waits on Sarp's approval |

Three more things hold before freeze.

- The 2026-09-25 amendment is approved and on main before the first live run.
  Otherwise `live_swapped` keeps the pre-registration's wording, and the plan
  changes back to #160's API adapter before freeze.
- `claude` is installed on the machine that runs step 4 and logged in with the
  Claude account whose plan carries the swapped arm
  ([bring_your_own_agent.md](../bring_your_own_agent.md#claude-code)).
- The #198 slot in `metadata.amendment_slots` is filled or stays empty. A
  natural failing cell joins only through a dated amendment appended to the
  pre-registration, with its fork point and four conditions added to the plan
  in the same commit, and only when its materialized `replay_mode_basis`
  records a block under `ctl_refund_window_v1`. A cell from the canonical or
  purchase_age family adds a pair and no independent family.
- `pytest tests/test_exp_001.py` passes on the tree about to be frozen. It
  rehearses a frozen copy of the plan, and while the plan is unfrozen it
  re-materializes each fork point, so a moved fixture or verifier shows up
  here, while the plan can still change.

Confirmed values are recorded in `needs_confirmation` in the commit that
confirms them.

### B1 at the registered fork points

Decided 2026-10-03. A blocking failure counts at or after the fork step in
both arms, and the pre-registration carries a dated amendment saying so. In
the offline rehearsal B1 is 0.0 at all three fork points. The analysis that
led there follows, unchanged.

B1 as first written could not see the violation the control prevents at
these fork points, and in the offline rehearsal it was null at two of the
three.

Two passages fix the behavior together. The pre-registration's arms table says
the `live` arm "continues from the recorded control step with the control
installed", and `live_no_control` is "the same fork with nothing installed".
Part B1 of `docs/methodology_metrics.md` says "a blocking failure counts only
checks whose `step_ids` fall after the fork step". The branch stage therefore
replays the recorded action at the control step in both arms
([branch_stage.md](../branch_stage.md#what-a-live-condition-does)). With the
control on, that action is blocked. With it off, the recorded
`unauthorized_cash_refund` fires at the fork step itself, which the rule leaves
out. The rehearsal shows it at steps 5, 4 and 3. At the two purchase_age fork
points that refund is the recording's only blocking failure, so the control-off
baseline is 0 of 5 and B1 is null. At refund_policy_failure the recording also
fails at steps 6 and 7, so B1 is 0.0 there. A live model changes only what
happens after the fork, so B1 never credits the control for the refund it
stopped.

The same reading of "after the fork" runs through the live verdict ("no
blocking failure after the fork", pre-registration Quantities) and both
divergence rates ("first post-fork tool call"). One function applies it to the
live verdict and to B1, and it counts a step as after the fork when it is
greater than the start step. With the control on, the blocked action fires
nothing at the fork step at any registered fork point, so the eight metrics do
not depend on this choice. B1 does.

B1 appears nowhere in the pre-registration or the brief, so for brief 001 it is
exploratory under the brief's experiment rule. Its definition belongs to the
#27 memo, and #203's keep rule reads it. The smallest options follow.

| Option | Change | B1 in the rehearsal |
|---|---|---|
| Keep the memo's rule | None. B1 reports what the agent does after the block and is null where the control-off continuation does nothing blocking after the fork | 0.0, null, null |
| Count checks at or after the fork step | "fall after the fork step" becomes "fall at or after the fork step" in Part B1 and the `ConditionViolations` docstring, in both arms. Keeping the live verdict consistent means a dated amendment to the pre-registration's "after the fork" before the first live run, though no registered live verdict changes | 0.0, 0.0, 0.0 |
| Count at or after the fork step in the control-off arm only | The same change for the baseline alone. The two violation rates then count different step ranges | 0.0, 0.0, 0.0 |
| Fork one step before the control step | Each live condition's `start.step_id` drops by one, so the model chooses the control-step action itself. This changes what every live arm measures, since a model that never attempts the refund records `no_block_observed`, and it needs a dated amendment to the pre-registration's arms table before the first live run | 0.0, 0.0, 0.0 |

Under the fixture, every option leaves the eight metrics as they are, since the
fixture's control-on continuation fails after the fork on every seed.

## Commands

From the repository root, with the package installed. Only step 4 calls a
provider. It needs `GEMINI_API_KEY` in the environment, which never enters the
repository, and a logged-in `claude` on PATH for the swapped arm. The swapped
arm needs no `ANTHROPIC_API_KEY`, and the Claude Code agent removes it from the
CLI's environment, so the plan carries the runs. Run step 4 from a plain
terminal. The agent also removes the variables that mark a nested Claude Code
session, such as `CLAUDECODE`, from the CLI's environment.

```bash
PLAN=docs/acceptance/experiments/exp_001_replay_validity/experiment.json
FP=docs/acceptance/experiments/exp_001_replay_validity/fork_points
RUNS=runs/exp_001
mkdir -p "$RUNS"
```

**1. Estimate the cost.** It prints each live condition's expected and high
cost and exits 1 if the high estimate passes the cap.

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
and freezing again after the fix. Pass or fail, it writes `harness_check.json`
beside the plan with the outcome, the plan's sha256 and frozen set, the commit
it ran on and the rehearsal's numbers, and step 8 commits it.

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
  earlier runs spent. The swapped arm runs last and spends nothing against the
  cap, but once the cap has stopped, its seeds are refused like any live seed.
- The swapped arm runs on the Claude plan the CLI is logged in with, which
  claude.ai and every other Claude Code session draw on too. Check the plan's
  usage before its three invocations. When the plan's limit is reached the
  agent stops the CLI and the seed ends incomplete, so a replacement seed is
  spent on it, and a pair can end insufficient. If that happens, stop, and
  report the affected conditions as the pre-registration's rules decide.
  Leave usage credits off, since use past the plan's limit would then be
  charged where the harness cannot see it.
- The swapped arm is branched once like every condition. It records no
  cassettes, so nothing refuses a second branch of it mechanically, and a
  second batch of it is not recorded.
- A seed whose run ends incomplete is replaced from seeds 5 to 9 inside the
  same invocation. A `setup_error`, where the harness failed around the run,
  is not replaced.
- The Gemini arms record cassettes under the experiment folder, one folder
  per arm. Recording never overwrites a cassette, so a condition is branched
  once.
  Branching it again exits 2 before any run and lists the cassettes that
  already exist, and the first batch is the one to record.
- An interrupted invocation writes no batch for the condition it was in, but
  its runs still count against the cap, priced from their traces, and its
  cassettes stay, so that condition is refused afterwards. A run interrupted
  before its first provider response has no recorded cost, and every later
  live seed is then refused as `budget_unenforceable`. Either way that
  condition has no batch for step 5, and the report names it as interrupted.

**5. Record** every batch, with decision review by human. The pairs come from
the `Record with` lines that step 4 logged.

```bash
trace-harness --runs-dir "$RUNS" experiment record "$PLAN" \
  --decision review --decided-by human \
  $(grep -oE -- '--condition [^ ]+=batch_[^ ]+' "$RUNS/branch.log")
```

It prints the eight metrics, any excluded pair with its reason, and where it
wrote `repair_effectiveness.json`.

**6. Retain** a copy of the runs dir and of the three recorded files beside
the plan. The script refuses if the recorded plan differs from the retained
one, and stops before copying if a file looks like a provider credential.

```bash
scripts/retain_exp_001.sh "$RUNS"
```

**7. Regenerate** `result.json` and the B1 sidecar offline from what was
retained. Both must print `identical`.

```bash
scripts/regenerate_exp_001.sh
```

**8. Commit** the retained folder, meaning `harness_check.json`,
`result.json`, `report.md`, `repair_effectiveness.json`, `runs/` and
`cassettes/`. The plan does not change, and `git log --oneline -- "$PLAN"`
still ends at the freeze commit. `tests/test_exp_001.py` then checks that the
retained harness check passed on the committed plan's exact bytes.

## Cost against the cap

`scripts/estimate_exp_001_cost.py` prices calls through the adapters' own
tables, `GEMINI_PRICING` and `ANTHROPIC_PRICING`, so a price change in code
changes its output. Token counts come from the retained cassette
`fixtures/cassettes/refund_policy_failure/gemini-3.6-flash/default.jsonl`,
whose five calls send 995, 1251, 1750, 2851 and 3573 input tokens and return at
most 690 output tokens. A call at step `s` is priced at the cassette's input for
step `s`, growing by 1101 tokens a step past step 5, and at 690 output tokens.

The numbers below are priced at 0.75 and 3.75 US dollars per million input and
output tokens for gemini-3.6-flash, and at 2 and 10 for claude-sonnet-5, the
prices in `GEMINI_PRICING` and `ANTHROPIC_PRICING`. The Sonnet 5 price is
Anthropic's published one, which #229 put in the table.
`tests/test_exp_001.py` recomputes this table from the script and checks that
the tables still hold the prices stated here.

| Arm | Model | Billing | Expected | High |
|---|---|---|---|---|
| `live` | gemini-3.6-flash | API, charged against the cap | $0.14 | $3.53 |
| `live_no_control` | gemini-3.6-flash | API, charged against the cap | $0.14 | $3.53 |
| Total against the cap | | | $0.28 | $7.05 |
| `live_swapped` | claude-sonnet-5 through Claude Code | Claude plan, notional | $0.38 | $9.40 |

The swapped arm runs on a Claude plan (the 2026-09-25 amendment), so it has no
per-run charge and records no `cost_usd`, and the cap binds on the Gemini arms
alone. Its line is what the same calls would cost over the API at list prices.
Each of its runs records the CLI's own estimate of that as `notional_cost_usd`,
which the cap never counts. The one Claude Code run made so far, of
`refund_policy_valid_cash` from its first step on 2026-09-25, took 5 steps and
reported $0.0254, including a Claude Haiku call the CLI made for itself. What
the swapped arm uses up is the plan's usage, which step 4 says to check.

Expected assumes five seeds per condition, each making as many calls after the
fork as the recording did. High assumes ten runs per condition, seeds 0 to 4
and every replacement, each running to the 16 step limit. Neither is a bound,
since a live call can return more output tokens than 690 and a live transcript
can grow faster than the recorded one. The Claude line uses Gemini's token
counts, since no Claude run of a fork point is retained. Pricing every call at
1010 output tokens, the largest single call in the eight retained 2026-09-13
live Gemini runs (`--output-tokens 1010`), gives $0.34 expected and $7.92 high
against the cap, and $0.46 and $10.55 notional. What bounds the spend is the
cap. The guard checks it between runs, so the overshoot past it is at most one
run.

## Stopping rules as the tools apply them

| Pre-registration rule | What happens |
|---|---|
| The harness check fails | Step 3 prints `FAIL`, `harness_check.json` records it, and no live run starts |
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
  `verdict_agreement_rate/live_swapped/claude_code_ref:claude-sonnet-5`.
- **`live_swapped`.** #200 lists it as a condition to run. The
  pre-registration runs it with #160's API adapter if that is on main before
  the first live run, and its 2026-09-25 amendment runs it through Claude Code
  on a Claude plan instead.
- **Harness check.** #200 does not name it. The pre-registration runs it
  first, and step 3 is it.
- **B1.** #200 asks for B1 per artifact and control. The sidecar has one entry
  per control-on arm, artifact, control and model, with the arm named in
  `arm`, and the swapped model's entries are null, since the pre-registration
  gives that model no control-off arm. An entry is also null when either side
  has fewer than five completed runs, the memo's minimum for B1.

## What the rehearsal shows about the numbers

- B1 is 0.0 at refund_policy_failure and null at the two purchase_age fork
  points, for the reason in [B1 at the registered fork
  points](#b1-at-the-registered-fork-points).
- `verified_failure_count` counts every failed run in every batch, static
  replays and the control-off arm included. The rehearsal counts 48.
- `sibling_failure_rate` counts siblings per replay. Three replays run two
  distinct siblings, `refund_policy_valid_cash` twice.

## Known limits

- The fork points were recorded by the fixture adapter and carry no provider
  state, and no test calls a live provider from such a prefix
  ([branch_stage.md](../branch_stage.md#limits)). If a provider rejects the
  prefix, every seed of that condition ends incomplete, the replacement pool
  runs out, and the pair is insufficient. The swapped arm gets the prefix as
  text in its first message, so it has no provider state to reject.
- The swapped arm reaches its model through Claude Code, whose loop, tool
  naming and message handling are its own. Its runs measure that agent with
  claude-sonnet-5 underneath, and its rate is reported under
  `verdict_agreement_rate/live_swapped/claude_code_ref:claude-sonnet-5`.
- The metrics history job scans `docs/acceptance` recursively and leaves out
  `docs/acceptance/batches` and `docs/acceptance/experiments`
  (`EXPERIMENT_EVIDENCE` in `metrics/history.py`, decision D4). Once step 8
  lands, the experiment's batch summaries and repair packages therefore stay
  out of its suite pass rate and control coverage.
