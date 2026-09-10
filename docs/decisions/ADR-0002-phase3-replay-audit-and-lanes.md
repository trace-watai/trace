# ADR-0002: Phase 3 opens with the replay-validity audit; replay verdicts are advisory until labeled; ownership moves to lanes

**Status:** Proposed (2026-09). Becomes Accepted on merge.

## Context

The offline product loop from ADR-0001 is built: 29 runnable tasks, a
deterministic verifier, heuristic attribution, failure bundles, pinned
regression artifacts, batch execution, and a dashboard reading real
artifacts. The AutoLab roadmap's Phase 2 is nearly closed and Phase 3, the
first autonomous experiment loop, is next. Three things had to be decided
before Phase 3 could start, and each had been argued more than once.

**Which experiment goes first.** The roadmap's default was judge-prompt,
guardrail-config, and retrieval-config experiments. Two facts changed that.
The human-labeled trace set that any judge experiment needs does not exist
(issue #31). And our own control validation has a hole: `replay
--apply-control` re-runs a *recording* with a guardrail installed, and a
recording cannot react to being blocked, so the "control works" verdict on
every regression artifact is unverified against a live agent. A literature
check (issue #28 carries the matrix) found the static-versus-live question
measured for model switching, for guardrail verdicts in control systems,
and for full-path authorization, but never for a pinned agent trajectory
with a control installed. That is the one open question our machinery can
answer cheaply.

**What a replayed verdict is worth.** The regression artifact is the
README's promised "pinned test to CI gate," but until the question above is
measured, its control verdict is a guess dressed as a gate.

**How work is routed.** Ownership has been per person: one named owner per
module, first reviewer by default. When that person was unavailable the
module stalled, and the July 28 review recorded that most tickets proved
only their own component because no one owned the seams. The remaining
work is mostly seams.

## Decision

1. **Phase 3's first research brief is the control replay-validity audit.**
   Freeze a recorded run at the control point, let live agents continue
   under each experiment condition, score them with the existing verifier,
   and compare with what the replay claimed. Tickets: #155 (experiment
   contract), #156 (replay-mode label), #157 (post-block outcome
   classification), #158 (brief and pre-registration), #159 (branch stage),
   #161 (regression CI collector). Judge-prompt and retrieval-config
   experiments follow once #31 delivers labels.

2. **A static replay verdict on a control is advisory until the artifact
   carries a measured replay-mode label.** #156 adds the label with a
   heuristic value at materialization; #159 replaces it with a measured
   one. The CI collector (#161) treats control results on `live_required`
   or `unlabeled` artifacts as advisory and gates only on `static_ok`. The
   plain reproduction check and positive siblings gate as before.

3. **Ownership moves from named module owners to five lanes.** Evaluation
   Core, Evaluation Systems, Frontend, Research and QA, TPM. Each lane owns
   a set of modules and has a reviewer pool; a change to a shared contract
   gets a second reviewer from the consuming lane, except that PRs authored
   by the project lead need one reviewer and docs-only ones from the lead
   merge on green gates. Any ticket untouched for
   48 hours is open to anyone in its lane. Review turnaround target is one
   day; PRs stay under roughly 300 lines with one contract change each.
   Tickets carry a lane, not a person; assignee fields on open tickets are
   cleared in favor of lane labels once this ADR merges.
   `docs/team_ownership.md` is rewritten accordingly.

## Why these over the alternatives

- *Judge or retrieval experiments first* (rejected): the judge has no
  labels to be measured against, and a retrieval experiment would be
  validated by the very replay whose validity is in question.
- *A study of real coding-agent sessions from public logs* (rejected): ADR-0001
  and the v0 scope exclude coding agents, and the audit needs an environment
  that restores exactly, which our sandbox provides and public repositories
  do not.
- *A memory-constraint protocol delivered over MCP* (rejected): each of its
  defining components has a published neighbor; see the matrix in #28.
- *Keep named module owners* (rejected): the bottleneck the July 28 review
  described is a routing problem, and a pool with a pickup rule fixes
  routing without changing who knows what.

## Consequences

**Positive:** Phase 3 starts on a question four recent papers left open,
with data we already produce and a cost of tens of dollars per sweep. The
regression gate stops overstating what it proves. Work no longer stalls on
one person's availability, and the five open control-loop issues (#143 to
#147) and the September tickets (#152 to #163) all carry a lane.

**Costs we accept:**
- The audit's novelty is narrow. It is a measurement paper about testing
  agents, not a capability result, and three groups published adjacent
  work in August 2026. The pre-registration in #158 exists so a null result
  is still publishable.
- Single domain. External validity rests on the live sweep producing
  failures we did not author and on a second domain later.
- Lanes trade "one person knows this area best" for availability. Module
  knowledge now has to live in `docs/modules.md`, which becomes load-bearing.
- Lane routing depends on labels being maintained; a ticket without a lane
  label is a ticket nobody owns.

**Revisit triggers:** a null result on brief 001 (fall back to the harness
as an engineering artifact with a workshop paper); the second workflow
environment landing; a required-review count being set on `main`, at which
point reviewer pools become enforceable rather than conventional.
