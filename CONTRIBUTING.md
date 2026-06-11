# Contributing to TRACE

This guide keeps ~13 parallel contributors from stepping on each other.
The architecture does most of the work (clear seams, typed contracts);
this document covers the rest.

## Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
scripts/check_repo.sh   # lint + format + tests + pipeline smoke — must pass
```

## Branches

One branch per workstream feature: **`<workstream>/<short-feature>`**,
lowercase, hyphenated. Include your Linear id when you have one —
`trace-harness/tra-12-runner-retries` — so Linear auto-links the PR.

Examples matching our workstreams:

```
evaluation-core/task-schema
evaluation-core/refund-boundary-variants
evaluation-core/verifier-citations
evaluation-core/judge-schema
trace-harness/runner-skeleton
trace-harness/rag-environment
trace-harness/typed-event-payloads
frontend/trace-replay
research/methodology
ops/ci-check-repo
```

Teams may keep broad integration branches, but avoid giant conflicting
PRs: land in slices. If your diff touches three workstreams, it should
probably be three PRs landed in dependency order.

## Pull requests

Every PR includes:

1. **A Linear ticket link** (`TRA-…` in branch name or description).
2. **Tests** for behavior changes — in the test file matching the module.
   New verifier checks additionally need *positive* (anti-overblocking)
   tests.
3. **Docs**: update `docs/modules.md` / the relevant `docs/*.md` when behavior
   they describe changes. Schema changes bump `schema_version` and update
   all consumers in the same PR — silent schema changes get reverted.
4. **Sample artifacts** when output shapes change (paste the relevant
   JSON snippet or attach a run-dir diff) so reviewers see the contract,
   not just the code.
5. **A passing `scripts/check_repo.sh`.**
6. **Expected-fixture honesty**: if `fixtures/expected/` changed, the PR
   body says why the expectation legitimately moved.

Keep PRs small and within your workstream's folders. Reviewers: the
folder's owner (see `docs/team_ownership.md`); add the TPMs
(Mohammed/Sarp) for anything cross-cutting, schema-versioned, or
ADR-worthy. Ask for TPM review by tagging the PR `needs-tpm` (or pinging
in the team channel) rather than letting it idle.

## Agent-session metadata (Entire.io)

We want TRACE's own history to be legible the way TRACE makes agent runs
legible — commits should carry the agent-session context that produced
them. **Entire is already enabled in this repo**: the guarded Claude Code
hooks in `.claude/settings.json` and the project config in `.entire/` are
committed, and they are no-ops unless you have the CLI installed.

To participate, install the CLI once
(`curl -fsSL https://entire.io/install.sh | bash`, see entire.io for
details) — your coding-agent sessions are then checkpointed automatically,
linked to your commits (look for the `Entire-Checkpoint` trailer), and
session metadata is pushed to the `entire/checkpoints/v1` branch alongside
your `git push`. Mention notable agent sessions in your PR description.
**If the CLI gives you trouble, do not block work — note it in the PR and
move on.**

## Cross-stream coordination (who must talk to whom)

- **Evaluation Core** (Emily, Evan He, Karan, Darrel, Samir) co-own the
  task ↔ verifier ↔ attribution schema chain: check ids, severity
  semantics, failure categories. Change one link, notify the chain.
- **Evaluation Systems** (Rupert, Evan Yang, Samrath) co-own the runner ↔
  environment ↔ trace contracts: the `ToolEnvironment` protocol,
  side-effect classes, event payloads. These three seams are where silent
  breakage would hurt most — PRs touching them get all three as reviewers.
- **Frontend** (Skye) builds against run artifacts: data-contract needs go
  to Samrath (trace/artifacts) and Darrel (attribution semantics) as
  tickets, not as dashboard-side workarounds.
- **Research/QA** (Justin, Katharine) audit assumptions and docs anywhere;
  findings land as issues/tickets against the owning area, with the owner
  as assignee.

## Code standards

- Python 3.11+, Pydantic v2 models for anything that crosses a boundary.
- `ruff check` and `ruff format` clean (line length 100).
- Determinism in fixture mode is sacred: no clocks in state, no
  randomness, no network in tests. CI runs with **no API keys** — code
  accordingly.
- TODOs carry an owner: `# TODO(Karan/verifier): …`. Honest TODOs beat
  fake completeness; unowned TODOs get deleted.
- Don't hardcode scenario content in `src/` — scenario content is fixture
  data. (There is no "Casey Nguyen" anywhere in the package; keep it that
  way.)

## Testing conventions

- Tests live in top-level `tests/` and run with bare `pytest` from the
  repo root. They are offline forever — a test that needs an API key or
  the network is a regression by definition.
- Tests consume the *real* fixtures under `fixtures/` (no synthetic
  copies that can drift) and write into pytest temp dirs, never `runs/`.
- Verifier unit tests use neutral synthetic customers; don't extend
  scenario fixtures just to make a unit test pass.
- Whoever owns a module owns its tests — new checks, event types, and
  artifacts all need tests in the matching `test_*.py`.
- `fixtures/expected/` files are pinned contracts: when a deliberate
  change breaks one, update it in the same PR and explain why in the PR
  body. Silent expectation updates get rejected in review.

## API keys

Copy `.env.example` to `.env` for local secrets; `.env` is gitignored.
Free Gemini keys are for early prototyping only — tests must never require
them, and the fixture provider stays the default. Sponsor credits replace
free keys when limits bite.
