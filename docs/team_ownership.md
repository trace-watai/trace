# Team ownership map

Ownership is by **lane**, not by person (ADR-0002). A lane owns a set of
modules, keeps their docs truthful, and supplies reviewers from its pool.
Nobody is a gatekeeper: everyone can contribute anywhere, and any ticket
untouched for 48 hours is open to anyone in its lane. If a row doesn't
match reality, change the row.

## Lanes

| Lane | Owns | Members |
|---|---|---|
| **Evaluation Core** | `trace_harness/tasks/`, `fixtures/tasks/`, fixture authoring standards; task variants and adversarial scenarios; `trace_harness/verifiers/`, `fixtures/expected/`, the policy-rules-as-data invariant; `trace_harness/attribution/`, judge schema and the labeled-set program; `trace_harness/failure_bundles/`, `trace_harness/regression/`, the regression CI gate | Emily Au, Evan He, Karan Gupta, Darrel Wihandi, Samir Mohammed |
| **Evaluation Systems** | `trace_harness/runner/`, `trace_harness/models/` (adapter contract, live adapters), `cli.py`; `trace_harness/environment/` (state, tools, retrieval, registry, guardrails, controls), `fixtures/docs/`; `trace_harness/tracing/` (events, recorder, artifact store, run index), `config.py`, the read path and future API (`docs/future_api.md`), data contracts to the frontend | Rupert Maiti, Evan Yang, Samrath |
| **Frontend** | the dashboard (`apps/dashboard/`, spec in `docs/future_dashboard.md`); TypeScript mirrors of every artifact schema; the offline fixture bundle | Skye Haik |
| **Research and QA** | `docs/verifier_philosophy.md`, `docs/attribution_methodology.md`, the metrics memo, the related-work matrix and claims memo, research briefs and pre-registration; docs quality across the repo; audit passes over fixtures and verifier assumptions; the human-labeled attribution set | Justin Lam, Katharine |
| **TPM** | architecture decisions (ADRs), cross-lane tradeoffs, this map; Linear hygiene, CI (`scripts/check_repo.sh` → GitHub Actions), release cadence, readiness decisions | Mohammed Elshrief, Sarp Doven |

## Reviews

- A PR is reviewed by someone from the **owning lane's pool**. The author
  cannot be that reviewer.
- A PR that changes a **shared contract** (a schema version, an artifact
  file, a trace event, a CLI flag, a TypeScript mirror) also gets a
  reviewer from the **consuming lane**.
- **Project lead exception.** PRs authored by the project lead (TPM lane)
  need one reviewer, from the owning lane, not two. Docs-only PRs from the
  project lead merge on green gates without a review. The lead still cannot
  approve their own PR; that is a GitHub rule, not ours.
- Review turnaround target: one day. PRs stay under roughly 300 lines with
  one contract change each; split otherwise.
- Branch protection on `main` requires the two CI gates and an up-to-date
  branch. It does not yet require an approving review; until it does, the
  rules above are convention.

## Cross-lane contracts (talk before you change)

- **Task ↔ verifier semantics** — Evaluation Core, internally: expected
  behavior wording, check ids, severity.
- **Runner ↔ environment ↔ trace** — Evaluation Systems, internally: the
  `ToolEnvironment` protocol, side-effect classes, event payloads.
- **Artifacts ↔ frontend** — Evaluation Systems + Frontend (+ Evaluation
  Core for attribution views): run-directory layout and schema versions.
- **Controls ↔ repair packages ↔ regression** — Evaluation Systems +
  Evaluation Core: `ControlInstance`, the prescribed-versus-executable
  map, the replay-mode label.
- **Methodology ↔ everything** — Research and QA audit assumptions; their
  findings file as issues against the owning lane.

Per-module working guidance (interfaces, rules, next steps) lives in
[modules.md](modules.md). With lanes replacing named owners, that file is
where module knowledge has to live.

## Tickets

- Every ticket names its **lane** in the body and carries the matching
  `workstream:*` label. Assignees are optional and never a precondition to
  start.
- Blocked tickets carry `status:blocked` and say what unblocks them.
- Every PR links its Linear ticket — the Linear GitHub integration picks
  up `TRA-123` in the branch name or PR description; branch names like
  `trace-harness/tra-12-runner-retries` do both jobs.
- Code TODOs carry the lane (`TODO(evaluation-core/verifier): …`) and get
  a ticket id appended once one exists.
- When a module's "build next" list ([modules.md](modules.md)) becomes
  real work, it becomes a ticket — the docs seed the backlog; Linear *is*
  the backlog.

Maintained by the TPM lane.
