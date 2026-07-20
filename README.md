# TRACE — Agent Reliability Harness

**WAT.ai · trace-watai**

TRACE runs AI agents through realistic workflows, captures structured
execution traces, verifies whether the outcome was *actually* correct,
localizes where the trajectory failed, and turns failures into durable
artifacts: failure cards, repair packages, and rerunnable regression
tests.

```
task → agent execution → structured trace → deterministic verifier
     → attribution/judge → failure card → repair package
     → regression test → dashboard
```

> **The verifier decides pass/fail. The judge explains why.**

Hard correctness is deterministic; LLMs may classify, summarize, and
propose repairs — never decide a release-blocking verdict. And diagnosis
of one run isn't the product: the regression lifecycle (failure → pinned
test → CI gate) is what makes agents *stay* fixed.

## Quickstart (no API keys needed)

Requires Python 3.11+.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                                                          # offline test suite
scripts/demo.sh                                                 # the end-to-end demo: both tasks, every artifact
scripts/check_repo.sh                                           # the pre-push bar (lint + format + tests + smoke)
```

`scripts/demo.sh` is the one-command demo / readiness check: it runs the full
pipeline on a staged failure (→ verifier FAIL + attribution + failure bundle)
and its positive sibling (→ PASS, proving no overblocking), writing every
artifact under `runs/demo/<run_id>/`. It's deterministic and offline — no API
keys, no network. To drive the pipeline directly instead:

```bash
trace-harness run-pipeline fixtures/tasks/refund_policy_failure.json    # staged failure → FAIL + full bundle
trace-harness run-pipeline fixtures/tasks/refund_policy_valid_cash.json # positive sibling → PASS
```

Pipeline stages also run individually, handing off through artifacts in
`runs/{run_id}/` (any stage can be re-run later):

```bash
trace-harness run-fixture fixtures/tasks/refund_policy_failure.json
trace-harness verify    runs/<run_id>            # → verifier_result.json
trace-harness attribute runs/<run_id>            # → attribution_result.json
trace-harness bundle    runs/<run_id>            # → card / repair / regression
trace-harness verify    runs/<run_id> --fail-on-verifier   # CI gate mode (exit 1)
```

## Repo map

```
src/trace_harness/      the harness (one module per pipeline stage)
  tasks/                task schemas + fixture loading
  models/               ModelAdapter protocol; fixture + Gemini adapters
  environment/          tools, typed state, retrieval, side effects
  runner/               AgentRunner execution loop
  tracing/              TraceEvent schema, JSONL recorder, ArtifactStore
  verifiers/            deterministic pass/fail with evidence
  attribution/          heuristic failure localization
  failure_bundles/      failure cards + repair packages
  regression/           pinned, rerunnable regression artifacts
  cli.py                trace-harness subcommands (one per stage)
tests/                  offline pytest suite — run with bare `pytest`
fixtures/               scenario data: tasks / docs / scripts / expected
runs/                   run artifacts (gitignored; layout in runs/README.md)
docs/                   architecture, methodology, module guide, ADRs
scripts/                run_fixture.sh, check_repo.sh
```

Working on a ticket? [docs/modules.md](docs/modules.md) has a section per
module: what belongs there, the rules, and what to build next.
[docs/team_ownership.md](docs/team_ownership.md) maps every area to its
owner.

## What's implemented now

One vertical slice, end to end and tested ([docs/first_vertical_slice.md](docs/first_vertical_slice.md)):
a support agent with `search_docs` / `get_order` / `issue_refund` /
`create_ticket`, a knowledge base with a current policy + deprecated trap
+ resolved-incident red herring, a scripted failure (deprecated policy
treated as authoritative → unauthorized $432 cash refund at 47 days →
false outage claim in a ticket), the deterministic verifier that catches
it with evidence, heuristic attribution (root cause step 3 ≠ first
irreversible action step 5), the generated failure bundle, and a passing
positive sibling proving no overblocking. Plus typed, versioned schemas
for every contract and a sub-second offline test suite.

## What's intentionally scaffolded (not faked)

- **Gemini adapter** — config-checked scaffold; the provider call raises
  `NotImplementedError` with the intended design. Fixture provider is the
  default everywhere; tests never need keys.
- **Refund guardrails** — deliberately absent so the verifier has real
  failures to catch; the installation seam is marked in
  `environment/support_env.py` and prescribed by the generated repair
  package.
- **Attribution** — rule-based, confidence-capped; the LLM judge comes
  later and must beat this baseline.
- **API & dashboard** — contracts and start conditions only:
  [docs/future_api.md](docs/future_api.md),
  [docs/future_dashboard.md](docs/future_dashboard.md).
- **Replay** — regression artifacts re-run their originating fixture;
  replay-from-pinned-state is a named next step.

Observability stance: TRACE-native artifacts (`trace.jsonl` + run JSON)
are the source of truth; Langfuse/Phoenix/OpenTelemetry are optional
future export layers, never local dependencies (see
[docs/architecture.md](docs/architecture.md)).

## Team ownership

| Area | Owner |
|---|---|
| Tasks & fixtures | Emily Au (+ Evan He: variants/red tasks) |
| Verifiers & expected outcomes | Karan Gupta |
| Attribution / judge | Darrel Wihandi |
| Failure bundles & regression | Samir Mohammed |
| Runner, model adapters, CLI | Rupert Maiti |
| Environment & retrieval | Evan Yang |
| Trace schema, storage, future API | Samrath |
| Dashboard / trace replay | Skye Haik |
| Methodology & research | Justin Lam |
| Audit, docs quality | Katharine |
| TPM | Mohammed Elshrief, Sarp Doven |

Details, cross-stream contracts, and Linear conventions:
[docs/team_ownership.md](docs/team_ownership.md).

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) (branch naming, PR expectations,
Linear linking, Entire.io session metadata). The short version:

1. Read your module's section in [docs/modules.md](docs/modules.md) first.
2. Schemas are versioned contracts — bump `schema_version` and update
   consumers + tests in the same PR; never change shapes silently.
3. Keep fixture mode deterministic and offline; a test that needs a key
   or network is a regression.
4. Small PRs, linked Linear ticket, tests included,
   `scripts/check_repo.sh` green.
5. If `fixtures/expected/` changes, say why in the PR body.
