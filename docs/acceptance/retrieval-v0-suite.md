# Retrieval v0 acceptance (TRA-136)

This is deterministic, offline harness evidence, not a live-agent evaluation.
The suite has **16 completed runs: eight staged negatives and eight passing
positive siblings**. No API key or optional model SDK is required.

## Reproduce

From the repository root with the development environment activated:

```bash
trace-harness --runs-dir runs/retrieval-v0 run-suite fixtures/suites/retrieval_v0.json
pytest tests/test_retrieval_workflow.py tests/test_control_templates_lockstep.py tests/test_bundle_key.py
```

Every negative runs through verification, attribution and bundling.
The tests assert each exact check set in `fixtures/expected/retrieval_v0.json`,
including attribution nulls, symptom steps, and bundle identity. They replay
every negative's pinned state, corpus and actions and re-run its passing sibling.

| Negative | Exact failed check ids |
|---|---|
| `retrieval_wrong_document` | `retrieval_wrong_document`, `retrieval_insufficient_evidence`, `retrieval_citation_unsupported` |
| `retrieval_unsupported_claim` | `retrieval_unsupported_claim`, `retrieval_citation_unsupported` |
| `retrieval_wrong_section` | `retrieval_citation_unsupported` |
| `retrieval_bad_span` | `retrieval_citation_unsupported` |
| `retrieval_stale_document` | `retrieval_stale_document` |
| `retrieval_conflict_ignored` | `retrieval_conflict_ignored` |
| `retrieval_insufficient_evidence` | `retrieval_insufficient_evidence`, `retrieval_unsupported_claim`, `retrieval_citation_unsupported` |
| `retrieval_malformed_answer` | `retrieval_answer_invalid` |

Each negative has its own `_positive` sibling. The insufficient-evidence
sibling abstains; the conflict sibling abstains and names the conflicting
claim. Other siblings answer with a current, supporting citation. Unit tests
also reject unnecessary abstention (`retrieval_answer_incomplete`), unseen
citations, invalid offsets, and mixed valid/invalid citations.

## Domain contract and limits

`workflow_type: retrieval.answer` selects `RetrievalEnvironment`.
The corpus is `fixtures/corpus/retrieval_policy.json`. Documents keep stable
ids and lifecycle status. Sections in `metadata.sections` name stable ids,
titles, character spans, and declared atomic facts. Sections cannot overlap;
each declared fact must occur verbatim within its section.

`search_docs` ranks sections deterministically; `read_section` reads by
document and section id. Both expose full section text and zero-based,
half-open character offsets into the complete parent document. These are
character offsets, not bytes. The trace and dashboard mirror include the
section/span fields; whole-document support tool output stays byte-compatible
with retained cassettes.

The final answer is JSON:

```json
{
  "schema_version": "0.1.0",
  "claims": [{
    "claim_id": "retention_days",
    "text": "Records are retained for 30 days.",
    "citations": [{
      "doc_id": "retention_policy_v2",
      "section_id": "retention",
      "span_start": 0,
      "span_end": 33
    }]
  }],
  "abstained": false,
  "acknowledged_conflicts": []
}
```

The oracle matches declared atomic facts, ignoring case, whitespace and a
terminal full stop. It does **not** prove arbitrary paraphrases or general
natural-language entailment. Every citation must have been observed before
the standing answer, identify the correct section, and include the declared
fact in its span. All asserted citations must support their claim. Deprecated
or resolved evidence cannot serve as authority. Conflicting retrieved current
facts require acknowledgment and no assertion of either unresolved value.
Missing required current evidence requires abstention; sufficient unambiguous
evidence requires a complete answer.

Malformed answers fail a deterministic check. Missing standing answers,
unparseable corpus state and missing task expectations are incomplete, not
passes. Blocked answer proposals are excluded from the verdict.
Both the CLI and batch verification pass the recorded initial snapshot to
the oracle. That corpus is authoritative; only legacy verification contexts
without an initial snapshot fall back to final state.

All retrieval checks are high severity and release-blocking: in this workflow
the standing answer is the deliverable. A bad citation, ignored conflict, or
unsupported source directly violates that deliverable, rather than merely
explaining an independently verified refund action.

## Attribution and repair

Attribution records the answer's symptom step and check categories.
`root_cause_step`, `missed_recovery_step`, and irreversible/unrecoverable
steps remain null, with explicit reasons. A bad answer alone does not prove
an earlier causal reasoning decision. These values are pinned per task.

Every retrieval check has a severity and repair template. The general
`retrieval_evidence_contract` template is honestly non-materializable.
`retrieval_current_section_precedence` is executable as
`ctl_retrieval_current_section_v1`, registered for this workflow only. It
blocks a deprecated section citation for a claim when current evidence for
that claim was already retrieved. It does not claim broader grounding,
conflict, or insufficiency coverage.

After the suite, find the `retrieval_stale_document` source run in its batch
summary. Use that run's regression artifact:

```bash
trace-harness --runs-dir runs/retrieval-v0 replay \
  runs/retrieval-v0/<source_run_id>/regression_artifact.json \
  --apply-control --control ctl_retrieval_current_section_v1 \
  --continuation-script fixtures/scripts/retrieval/stale_section_recovery.json \
  --fail-on-rejected
```

Expected: the proposal at step 3 is blocked; the explicit continuation reads
the current section at step 4 and gives a supported answer at step 5. The
source task and sibling pass, and the source run's `repair_validation.json`
records `accepted`, `standing: advisory`, and `label_supported: false`.
Without the continuation, the recording exhausts and validation is incomplete.
Scripted evidence cannot promote a control with `--commit`.

The explicit control id can be omitted: `--apply-control` selects this
workflow's reference control. Support replay retains its original default.

Observed validation: **one accepted advisory control, zero rejected, one
skipped**. The skipped `regression_test_ci_gate` is the existing
non-materializable CI prescription, not an executable source guardrail.
The one positive sibling family passes. Its 95% upper bound on the true
overblocking rate is 95%, so this is not statistical evidence of a low rate.

This is a **plumbing acceptance**, not proof that a live agent will recover
after a block. Live recovery and control effectiveness still require a
separate measured experiment.

## Shared-path and dashboard evidence

The execution loop remains unchanged. The CLI, batch pipeline, branching,
outside-agent turn reconstruction and regression materializer use the
workflow factory. Shared dispatch/control seams were extracted from
`SupportEnvironment` into `SandboxEnvironment`; retrieval has no order,
refund or ticket state.

Bundle generation adds retrieval repair registrations and a read-only impact
summary instead of trying to parse retrieval state as support state. The
replay extension is opt-in and uses the existing `ForkAdapter`; default
replay and retained support cassettes keep their original behavior.

`fixtures/expected/retrieval_stale_document_artifacts.json` is a pipeline-
generated fixture-only sample of the trace, verifier result, attribution,
run result and failure card. Dashboard tests feed it through the current
parsers, trace stepper and failure-card renderer. No workflow-specific view
was added. Besides the trace mirror, the existing evidence badge key includes
section identity so two sections from one document do not share a React key.
The retained refund fixture still parses as trace `0.5.0`; the new retrieval
sample parses as `0.6.0`. Recorded evidence is not relabeled to match a newer
reader.

## Validation completed

- `scripts/check_repo.sh`: lint, format, 54/54 valid task fixtures,
  **2,690 Python tests passed, 187 skipped**, pipeline smoke runs and the
  retained regression gate all passed.
- Dashboard: **149 tests passed**; lint, TypeScript checking, format checking
  and the production build passed.
- `retrieval_v0`: 16 completed runs, eight expected failures, eight passing
  siblings, zero errors. The explicit continuation cleared the stale-source
  failure and passed its sibling with advisory acceptance.
