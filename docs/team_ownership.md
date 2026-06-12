# Team ownership map

Who owns which part of the repo. "Owner" here means **steward and first
reviewer** — the person who knows the area best and keeps its docs
truthful — not a gatekeeper, and not a claim already staked. Nothing in
this repo is anyone's territory until they've actually picked it up;
everyone can contribute anywhere, and this map is expected to be redrawn
as people discover what they actually want to work on. If a row doesn't
match reality, change the row.

## Evaluation Core

| Person | Role | Owns |
|---|---|---|
| **Emily Au** | Task / benchmark design lead | `trace_harness/tasks/`, `fixtures/tasks/`, fixture authoring standards |
| **Evan He** | Task variants / red-task support | task variant tooling, adversarial/red scenarios under `fixtures/`, boundary sweeps |
| **Karan Gupta** | Verifier / oracle logic lead | `trace_harness/verifiers/`, `fixtures/expected/`, policy-rules-as-data invariant (with Emily) |
| **Darrel Wihandi** | Attribution / judge logic lead | `trace_harness/attribution/`, judge schema + labeled-set program |
| **Samir Mohammed** | Repair / failure cards / regression | `trace_harness/failure_bundles/`, `trace_harness/regression/`, regression CI gate |

## Evaluation Systems / Trace Harness

| Person | Role | Owns |
|---|---|---|
| **Rupert Maiti** | Target-agent runner / orchestration | `trace_harness/runner/`, `trace_harness/models/` (adapter contract, Gemini implementation), `cli.py` |
| **Evan Yang** | Tool / RAG environment & vertical-slice integration | `trace_harness/environment/` (state, tools, retrieval, registry), `fixtures/docs/` corpora |
| **Samrath** | Trace schema / logging / backend / storage | `trace_harness/tracing/` (events, recorder, artifact store), `config.py`, future API (`docs/future_api.md`), data contracts to frontend |

## Frontend / Dashboard

| Person | Role | Owns |
|---|---|---|
| **Skye Haik** | Trace replay / failure UX | the dashboard (spec: `docs/future_dashboard.md`) — builds against static run artifacts first; data-contract changes go through Samrath (trace/artifacts) and Darrel (attribution semantics) |

## Research / QA / Docs

| Person | Role | Owns |
|---|---|---|
| **Justin Lam** | Research / methodology / deterministic evaluation | `docs/verifier_philosophy.md`, `docs/attribution_methodology.md`, methodology review of verifier/judge designs, AgentRx positioning |
| **Katharine** | Human audit / documentation / research ops | docs quality across the repo, audit passes over fixtures + verifier assumptions, the human-labeled attribution set (with Darrel/Justin) |

## TPM

| Person | Role | Owns |
|---|---|---|
| **Mohammed Elshrief** | Product & technical direction | architecture decisions (ADRs), cross-stream tradeoffs, this ownership map |
| **Sarp Doven** | Execution / operations / delivery | Linear hygiene, CI (`scripts/check_repo.sh` → GitHub Actions), release cadence |

## Cross-stream contracts (talk before you change)

- **Task ↔ verifier semantics** — Emily + Karan (+ Darrel when categories
  are affected): expected_behavior wording, check ids, severity.
- **Runner ↔ environment ↔ trace** — Rupert + Evan Yang + Samrath: the
  `ToolEnvironment` protocol, side-effect classes, event payloads.
- **Artifacts ↔ frontend** — Samrath + Skye (+ Darrel for attribution
  views): run-directory layout and schema versions.
- **Methodology ↔ everything** — Justin + Katharine audit assumptions;
  their findings file as issues against the owning area.

Per-module working guidance (interfaces, rules, next steps) lives in
[modules.md](modules.md).

## Linear conventions

The team space uses the `TRA-` prefix (TRA-34 produced
`AGENTRX_TRACE_SUMMARY.md`). Each ownership row above is a workstream;
fill in its tracking ticket ids as they are created.

- **Every PR links its Linear ticket** — the Linear GitHub integration
  picks up `TRA-123` in the branch name or PR description; branch names
  like `trace-harness/tra-12-runner-retries` do both jobs.
- One workstream ↔ one long-lived area of the repo; tickets are scoped
  inside it. Cross-area tickets name both owners.
- Code TODOs carry the owner role (`TODO(Karan/verifier): …`) and get a
  ticket id appended once one exists
  (`TODO(Karan/verifier, TRA-123): …`).
- When a module's "build next" list ([modules.md](modules.md)) becomes
  real work, it becomes a ticket — the docs seed the backlog; Linear *is*
  the backlog.

Maintained by Sarp (ops) with Mohammed (direction).
