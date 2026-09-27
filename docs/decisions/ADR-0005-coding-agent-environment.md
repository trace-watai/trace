# ADR-0005: TRACE adds a coding-agent environment beside the refund workflow; real supervision sessions become labels, not tests

**Status:** Proposed (2026-09-26). Becomes Accepted on merge.

## Context

ADR-0001, ADR-0002 and ADR-0004 each name a second environment as a revisit
trigger, and [pre-registration 001](../experiments/preregistration/001.md)
shows why: the refund workflow yields 2 independent families with a block,
at most 6 under any registry, where certifying a rate under 5% needs 59.
ADR-0002 rejected a study of real coding-agent sessions because "ADR-0001 and
the v0 scope exclude coding agents" and the audit needs an environment that
restores exactly. That still holds for real sessions. A coding sandbox that
TRACE authors, pins by tree hash and checks offline can restore its files
exactly; whether its verdicts are as stable as `SupportState`'s is for the
authoring checks in (1) to show, not to assume.

Two gaps TRACE names are open: no human-labelled trace set (#31), and
step-level attribution at 11 to 14% in the literature brief 001 cites. The
`attention-forecasting` research project (its PLAN.md v2, decisions D018 and
D019) parses real Claude Code and Codex sessions (TraceLab, 8,058 sessions
from 52 developers, CC BY 4.0; DataClaw, MIT) and GitHub Copilot cloud-agent
sessions (AgentLogs, CC BY 4.0, labelled per session rather than per action).
It labels each state-changing action from what the person did next: they
interrupted, pushed back (including asking to undo it), refused a permission
prompt, or moved on without pushback. It plans to train forecasters of human
intervention under 1 GB: before the agent's next action runs, predict whether
the person will interrupt or push back. None is trained yet. It has a
converter to TraceEvent. Hindsight labels cannot show that acting on a
forecast helps; brief 001's live-continuation method can test that.

## Decision

1. **TRACE adds a coding-agent environment family and replaces nothing.**
   This lifts the v0 exclusion of coding agents that ADR-0002 cites, for
   environments TRACE authors and pins, and keeps ADR-0002's rejection of
   real sessions as test environments. The verifier-decides rule,
   TraceEvent, the controls library, brief 001's method and the lanes carry
   over.

   A coding task has a repository snapshot pinned by git tree hash (small,
   permissive, stdlib-only in Phase 0, checks run by `unittest` so nothing
   needs installing); a task statement, and a hidden requirement for the ask
   arm (5) kept out of `description`, `goal` and `metadata.user_message`,
   the fields the transcript builder reads; policy rules as data in the
   snapshot (protected paths and branches, allowed hosts), read as
   `RefundPolicyRules` is; and a deterministic verifier over final state:
   pinned checks pass, protected paths untouched, no push to a protected
   ref, no force push, no send outside the allowlist, and a final answer
   consistent with the checks. Positive siblings cover edits beside a
   protected path and pushes to a feature branch.

   Determinism needs care `SupportState` never did. Git author, committer
   and dates are pinned and user config is ignored, so commit ids repeat.
   `snapshot_state` records base tree hash, final diff, refs and check
   outputs with timings stripped, and the verifier reads those recorded
   outputs instead of re-running checks, so a run verifies identically
   forever. A check that disagrees with itself across five authoring runs
   rejects its task, since flakiness is over-blocking; five runs catch only
   gross flakiness (a check that flips 10% of the time passes all five 59%
   of the time). Side effects are declared per tool, as the runner's
   `side_effect_for` takes a tool name:

   | Tool | `side_effect` | Why |
   |---|---|---|
   | `read_file`, `list_files`, `search_code`, `run_check`, `http_get` | `read_only` | Checks run in a scratch copy; `http_get` reads a recorded stub |
   | `edit_file`, `write_file`, `delete_file`, `git_commit`, `git_branch` | `external_durable` | A diff or local ref the snapshot undoes |
   | `git_push`, `http_send` | `external_irreversible` | A local bare remote and a recorded stub stand in for what others see |

   Free-form shell waits for a per-call side-effect seam (one `bash` tool
   covers `ls` and `git push --force`). No tool reaches the network, but
   `run_check` executes code the agent wrote, so CI runs coding tasks only
   under scripted fixture agents, and live runs wait for an OS-level sandbox
   chosen in Phase 1; a container would amend ADR-0001 (decision 7). The
   environment satisfies the runner's `ToolEnvironment` protocol as it
   stands and reuses the tool types in `environment/` without editing them.

2. **The agent runs inside TRACE's runner first.** Through Phase 2 a model
   drives these tools via `ModelAdapter` (fixture, Gemini, Anthropic, OpenAI;
   #160), so a fork at a recorded step works as in brief 001. Claude Code,
   Codex and similar CLIs own their loops. In Phase 3 they run from the
   task's start in the sandbox and the same verifier judges their final
   state, so their runs are scored, not analysis-only. Their logs enter
   through a TRACE-owned importer; the research converter (3) is a
   reference, not a dependency. They need network egress to their model
   provider, which (1) forbids today, so a per-host allowlist is a Phase 3
   decision. Forking a CLI agent mid-run is an open question.

3. **Real sessions are analysis-only.** The research's converter
   (`trace_bridge`) emits TraceEvent 0.4.0 that validates under the current
   0.5.0 models: tool calls as `tool_call_requested`, results as
   `tool_call_executed` plus `tool_observation` with `side_effect` read from
   the command text (edits durable; `git push`, `rm -rf` and POST
   irreversible; unclear left null), agent text as `model_action`. TRACE
   has no event type for a person's message arriving mid-run (user-role
   messages exist only inside `model_prompt.new_messages`, which the
   converter does not emit), so interrupts, pushback and refused permission
   prompts (where the harness writes the refusal into the tool result, as
   Claude Code does) become `decisive_step_candidates` in the metadata of
   the event emitted just before them. Approvals are not visible in these
   sessions and are not claimed. `run_finished.status` is `analysis_only`,
   which the payload's plain-string field accepts. The import writes
   `trace.jsonl` only: `RunReader` lists runs by `run_result.json` and
   `task.json`, and `RunStatus` has no `analysis_only`, so Phase 0 reads
   these directories through a separate trace-only reader, and a new status
   value is a schema bump that waits (7). These runs get no
   `verifier_result.json` and never enter a suite, regression, the CI gate
   or a pass rate. Only permissively licensed, scrubbed sessions are
   committed to TRACE. The converter stays in the research repository.

4. **Real-session labels form a second, separately reported set beside
   #31.** #31's set (planned as `docs/acceptance/attribution_labels_v0.jsonl`
   and scored by C1; the file does not exist yet) stays as scoped, and this
   set does not close #31. The real-session set marks the step a person
   reacted to: the last state-changing action within 120 s before an
   interrupt or pushback. A rule picks that step from a real reaction; no
   person marked it, so it is a proxy label. It is none of
   `root_cause_step`, `missed_recovery_step` or
   `first_irreversible_action_step`, so it gets its own metric, C3
   (reaction-step agreement), never averaged with C1 or C2. The set comes
   from the research's labeller, not from the converter metadata in (3),
   which marks the preceding event instead. The heuristic attributor raises
   without a failed verifier result, so C3 needs an entry point that takes
   a candidate step and feeds no card or regression. The set enters only
   after the research's label-validity study passes (kappa lower bound 0.60
   against a blind multi-vendor panel, then a human subset) and its
   reaction-step agreement clears a threshold that study pre-registers, for
   permissive content, as hashed labels.

5. **Forecaster-driven ask-first controls are tested with brief 001's
   method, in brief 002.** In Phase 1 `BehaviorOnFailure.action` (in
   `environment/controls.py`, today `Literal["block"]`) gains `ask` (control
   library 0.2.0), and a trace bump records the pause; the answer already
   fits `model_prompt.new_messages` as a user-role message. The control
   pauses before a state-changing action when a forecaster crosses a
   pre-registered threshold. The forecaster is pinned by SHA-256, runs
   offline on CPU and installs as an optional extra, as Gemini does; tests
   use a stub scorer, so CI never needs weights. The mapping from TRACE tool
   calls to the forecaster's input encoding and the task families are
   frozen, with hashes recorded, before the forecaster scores any of them,
   so no task is authored toward where it fires.

   Brief 002 (the research's Study 07) forks where the control fires
   (#159's branch stage) and runs static replay with the control, live with
   no control, live with the ask answered by a scripted responder returning
   the hidden requirement, and live with an empty answer, which equals a
   block. Because the responder hands over the hidden requirement, ask
   versus no control mostly measures the value of that information. So
   whole-task live runs also compare the forecaster-driven control with one
   that asks at the same rate without it (the rule baseline or random
   steps; the pre-registration picks), and only that contrast credits the
   forecaster. The responder is a simulated answer, the weakness HiL-Bench,
   Ask or Assume? and CLARITI share, so brief 002 claims nothing about how
   real people answer. The verifier judges every arm, with
   `sibling_failure_rate`, ask rate on siblings and `post_block_outcomes`
   (#157, extended to asks) beside it. The pre-registration sizes n in
   families by a power calculation and reports k / n with Clopper-Pearson
   bounds; if the families available at registration fall short, brief 002
   registers as an existence test, as ADR-0004 did for brief 001.

6. **Doppl is a deployment surface, not a dependency.** TRACE's part ends
   at a control library entry that is `active` (promoted by
   `replay --apply-control --commit`) with a measured replay-mode label, a
   reported sibling rate and a reported ask rate. Whether and how Doppl
   ships such a control is Doppl's decision, recorded outside TRACE. TRACE
   depends on no Doppl code or data and on no research-repository code. One
   TPM builds Doppl and leads the research, so this ADR, brief 002's
   pre-registration and any promotion of a forecaster-driven control each
   need approval from the other TPM and a Research and QA reviewer; the
   project-lead one-reviewer exception does not apply to them.

7. **Brief 001's frozen paths do not move before its report merges.** The
   `refund_v0` and `refund_bundles_v0` suites, `fixtures/tasks/`,
   `fixtures/scripts/`, `fixtures/expected/`, `src/trace_harness/verifiers/`,
   `src/trace_harness/environment/` and `scripts/check_repo.sh` stay as
   registered. Brief 001's allowed registry changes (`controls.py`,
   `guardrails.py`, `fixtures/controls/library.json`) get no coding entries.
   `src/trace_harness/regression/` and `replay` produce brief 001's static
   verdicts and build `SupportEnvironment` directly, so coding regression
   artifacts wait for the report too. Coding work lives in
   `src/trace_harness/coding/` (own registries and its own pipeline entry,
   since `cli.py` and `runner/pipeline.py` also build `SupportEnvironment`
   directly), `fixtures/coding/` and `tests/test_coding_*.py`, which bare
   `pytest` collects with no gate change; the five-run authoring check is a
   script, not a test, so the gate stays fast. Phase 0 edits nothing in
   `runner/`, `models/`, `tracing/` or `cli.py`; a change there that brief
   001's arms execute lands before its first live run or after its report.
   Schema bumps, the per-call seam and registry merges wait for the report,
   and coding fork points never enter brief 001's sample.

8. **Lanes own the new work.**

   | Lane | New work |
   |---|---|
   | Evaluation Core | Task families and siblings; the coding verifier and its positive tests; the C3 entry point; `ask` semantics, with Evaluation Systems, since the controls contract is shared |
   | Evaluation Systems | Sandbox, tools, tree-hash pinning and git determinism; the trace-only reader; adapters; both bumps; the live-run sandbox; review of the research converter |
   | Frontend | Analysis-only runs, decisive-step markers on the timeline, a diff panel |
   | Research and QA | C3 in the metrics memo; intake of the real-session label set; brief 002 and its pre-registration; consent protocol; #28 update |
   | TPM | Phase gates, data-handling rules, liaison with the research and Doppl, under the review rule in (6) |

9. **Work lands in phases, one ticket per row, each with a lane. This term
   commits to Phase 0 only.** *Phase 0, while brief 001 runs*, is sized so
   each row is one PR under roughly 300 lines (ADR-0002). Core is
   Evaluation Core, Systems is Evaluation Systems.

   | # | Lane | Ticket | Needs |
   |---|---|---|---|
   | 1 | Systems | Coding state: load a fixture repo, check its tree hash, pin git identity and dates; snapshot base hash, diff and refs | |
   | 2 | Systems | Read-only tools and `run_check` (`unittest` in a scratch copy, timeout, timings stripped) | 1 |
   | 3 | Systems | Durable tools: edit, write, delete, commit, branch | 1 |
   | 4 | Systems | `git_push` to a local bare remote; `http_get` and `http_send` stubs | 1 |
   | 5 | Core | Policy rules from the snapshot; path and ref checks with positive tests | 1 |
   | 6 | Core | Send-allowlist and answer-consistency checks with positive tests | 4, 5 |
   | 7 | Core | Five-run authoring script that rejects self-disagreeing tasks | 2 |
   | 8 to 10 | Core | One family each: failing task, positive sibling, fixture scripts | 2 to 7 |
   | 11 | Systems | Coding pipeline entry in `coding/`: task to verified run | 2 to 6 |
   | 12 | Systems | Trace-only reader for analysis-only directories, no `RunStatus` change | |
   | 13 | Frontend | Analysis-only view with decisive-step markers | 12 |
   | 14 | Frontend | Diff panel for coding runs | 11 |
   | 15 | Core | C3 entry point: candidate step in, agreement out, no card or regression | |
   | 16 | Research and QA | C3 in the metrics memo; consent protocol; #28 update with the threats below | |
   | 17 | Systems | `docs/trace_schema.md` from 0.4.0 to 0.5.0 (docs only) | |

   *Phase 1, after brief 001 reports:* registry merges; coding regression
   artifacts; the per-call seam and a shell tool; `ask`, the trace bump and
   the responder; a live-run sandbox; live coding runs. *Phase 2:* brief
   002, once the research freezes a forecaster; the real-session set once
   label validity passes. *Phase 3:* CLI agents. Phases 1 to 3 have no
   date. Phase 1 starts only if Phase 0 is done when brief 001 reports;
   otherwise the research's Study 07 falls back to its transfer test on the
   refund tasks, which still needs `ask` from (5) but no coding environment.

## Evidence and limits

The converter's output validates against TRACE's models at a32f14a (36 tests
in the research repository, run 2026-09-24), but has no `model_action` for
tool-call steps. Pushback detection is a keyword rule of unmeasured
precision; the labeller detects requests to undo, not reverts in the
repository; and TraceLab has no text, so its reactions are a lower bound. The
rubric's Fleiss kappa of 0.873 (3 blind LLM annotators, 90 messages) shows
the panel agrees with itself, not that it is right. Studies 01 and 06 have a
committed, reported run; Study 02 rates labels but its final verdict awaits
the author's review of AI-suggested labels; Study 04 has descriptive point
estimates only, with its registered bootstrap run still queued; Study 03 has
frozen its data split but has trained no model; Study 05 has not started.
Brief 001's live arms wait on #159 and #157, neither merged as of this
proposal. Brief 002 can show whether a control helps on TRACE's tasks, not
in the real sessions its forecaster learned from.

The research's novelty check (2026-09-24) found the claim survives only
narrowed: a pre-action forecaster of human intervention (interrupt or
pushback) for coding agents, trained on hindsight labels from real Claude
Code and Codex sessions, under 1 GB and 10 ms p95, with an ask-first control
built from it tested by live continuation. The method is not part of that
claim: it is brief 001's, and fork-and-continue with over-blocking checks is
prior art (The Replay Gap, arXiv 2608.08239; Causal Agent Replay, arXiv
2606.08275). The closest threats are SAFETY SENTRY (arXiv 2607.13594), a
learned EXECUTE/ASK/REFUSE gate with over-ask rates trained on LLM-annotated
synthetic tasks; SWE-chat (arXiv 2604.20779) and How Coding Agents Fail
Their Users (arXiv 2605.29442), descriptive studies of the same kind of data
that name live pushback detection as future work; and SWE-Together (arXiv
2606.29957). A coding-agent system also named TRACE (arXiv 2606.13174)
compiles user corrections into runtime checks; any joint paper
disambiguates at first mention.

## Why these over the alternatives

- *Transfer test on refund tasks only* (kept as fallback): it tests whether
  a coding-trained forecaster transfers, not whether the control helps.
- *Replace the refund workflow* (rejected): it voids brief 001.
- *Real sessions as test environments* (rejected, as in ADR-0002): a
  person's reactions and a session's external effects do not restore, and
  no deterministic verifier can judge them.
- *A public coding benchmark* (deferred): its checks ask whether the fix
  works, not whether a boundary was crossed, and it has no positive
  siblings.
- *SWE-Together's environments* (arXiv 2606.29957, deferred): it rebuilds
  repositories from real DataClaw, pi and SWE-chat sessions at pinned
  commits, with a live agent and an LLM user simulator, but starts from the
  first request, is scored by a rubric judge and has no positive siblings.
  Its repositories could seed families where licences allow, with TRACE's
  verifier in place of the judge.

## Consequences

**Positive:** a domain where independent families can be authored for the
purpose; a route toward the n ADR-0004 says a rate needs, though one term
will not author 59 families; and rule-derived reaction-step labels from real
sessions as a second set beside #31.

**Costs we accept:**
- Scope. A second environment is a second product for a part-time student
  team. This term commits to Phase 0 alone, capped at three families and
  sized in (9); the gate stops Phase 1 if they are not done, and no frozen
  path moves, so brief 001 cannot be the casualty. Two label sets and two
  contract bumps must be kept apart.
- Verifier cost and safety. A coding verdict runs a test suite, so each run
  costs CPU minutes as well as tokens, and authoring runs every check five
  times. Stdlib-only repos keep Phase 0 cheap; each brief sets a cost cap,
  as pre-registration 001 does at 50 US dollars. `run_check` executes
  agent-written code, which is why CI runs scripted agents only and live
  runs wait for a sandbox.
- Consent. The team's Entire sessions on `entire/checkpoints/v1` are the
  best in-domain data TRACE could use, and a shared branch is not consent
  to research use. They are used only with each member's written,
  revocable opt-in, processed locally, reported only in aggregate, and
  never released or trained into a released model. The repository is
  public and appears among the research's sampled Entire repositories, so
  the research excludes it from that crawl until opt-ins exist.

**Revisit triggers:** a null result on brief 001; label validity failing
(the real-session set stays out); Phase 0 missing its gate; flaky checks
destabilizing coding verdicts; brief 002's power calculation needing more
families than the team can author; a published pre-action forecaster of
human intervention for coding agents; a way to fork a CLI agent mid-run.

## Reviewer notes

Evaluation Core review, 2026-09-24, against `trace-snapshot` at a32f14a.

**Changed**
- Claims narrowed to the novelty verdict: "forecasters of human
  intervention", not "the person will reject this"; "approved" dropped;
  "reverted" became "asking to undo" (the labeller has only a keyword rule,
  no repository-revert detector); "trains" became "plans to train"; "verified"
  became "tested"; "#31 labels from real people" became rule-derived proxy
  labels that do not close #31. The method is credited to brief 001 and
  prior art, and the top threats and the arXiv 2606.13174 name clash added.
- Repository facts corrected: `attribution_labels_v0.jsonl` does not exist
  yet; library entries are `active` or `rolled_back`, never `accepted`; the
  converter marks the preceding event, not the 120 s step; user messages can
  appear in `model_prompt.new_messages`; `RunReader` needs `run_result.json`
  and `task.json` and `RunStatus` lacks `analysis_only`, so a trace-only
  reader was added; `cli.py`, `runner/pipeline.py` and
  `regression/materializer.py` build `SupportEnvironment` directly; #157 and
  #159 are unmerged.
- Brief 001 protection widened: no coding entries in its allowed registry
  paths; `regression/` and `replay` treated as frozen for coding; no
  `runner/`, `models/`, `tracing/` or `cli.py` change between its first live
  run and its report.
- The v0 coding-agent exclusion is now lifted explicitly, since ADR-0002
  relies on it. The snapshot has no written v0 scope document stating it.
- Feasibility: "restores as exactly as `SupportState`" softened; git and
  timing determinism added; the verifier reads recorded check outputs; the
  five-run limit quantified; agent-written code execution and "network off"
  narrowed; a container would amend ADR-0001.
- Phase 3 contradiction fixed: sandboxed CLI runs are scored, not
  analysis-only, and TRACE does not depend on the research converter.
- Brief 002: a matched-rate comparison control added, since the responder's
  hidden requirement makes ask versus no control near-certain by
  construction; tasks and tool mapping frozen before the forecaster scores
  them; forecaster as an optional extra with a stub in tests; an
  existence-test fallback.
- Doppl: a TRACE ADR cannot bind what Doppl ships; a conflict-of-interest
  review rule added.
- Phase 0 split into 17 tickets under the 300-line rule; this term commits
  to Phase 0 only. Consent now covers the research's Entire crawl, which
  samples `trace-watai/trace`. SWE-Together added as an alternative.

**Verified unchanged:** the revisit triggers, the pre-registration 001
numbers, the frozen-path list, `side_effect_for(tool_name)`,
`Literal["block"]`, `TRACE_SCHEMA_VERSION` 0.5.0 against the doc's 0.4.0,
plain-string `run_finished.status`, C1 and C2 (so C3 is free), bare
`pytest` in `check_repo.sh`, the 50 US dollar cap and `RefundPolicyRules`.

**For the team to decide**
1. Live-run isolation: a container (amends ADR-0001), an OS sandbox, or no
   live coding runs this term.
2. Brief 002's comparator: the rule baseline, random asks at a matched
   rate, or both, and which contrast is primary.
3. The reaction-step agreement threshold for the real-session set, to be
   fixed in the research's Study 02 pre-registration.
4. Whether to seed families from SWE-Together repositories (licences, and
   replacing its judge) or author from scratch.
5. Who approves under (6), and who is the project lead for the exception.
6. The consent protocol's form, and whether TRACE adds a licence file. It
   has none, so under the research's licence rule its sessions are
   aggregate-only anyway.
7. Paper naming against arXiv 2606.13174.
8. Whether the ticket table moves to Linear on merge. The ADR body is
   about three times ADR-0002's length and could be trimmed.
