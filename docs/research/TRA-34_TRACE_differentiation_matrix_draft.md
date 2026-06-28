# TRACE Related-Work and Differentiation Matrix

Purpose: decision-oriented draft for TRA-34. This matrix separates direct trace-attribution competitors, broader agent benchmarks, infrastructure, and benchmark-validity methodology. The goal is to show how TRACE is different from adjacent agentic benchmarking work, not to produce a broad literature survey.

## Differentiation thesis

TRACE is not just another agent benchmark leaderboard and not just a trace viewer. TRACE should be positioned as a reliability evaluation harness that combines:

1. valid, reproducible, objectively verifiable tasks;
2. deterministic verifier evaluation, including false positives and false negatives;
3. LLM-assisted trace-level attribution, scored separately from deterministic correctness;
4. repair, overblocking, and regression metrics;
5. audit-ready reports that explain what failed, why it failed, and what engineering action follows.

Most adjacent systems cover only one or two of these pieces. TRACE’s differentiation is the combination.


## Main matrix

| Source / system | Category | Is it an agent-evaluation system? | What it mainly evaluates | Task / data type | Verifier or scoring approach | Trace-level attribution? | Repair / regression support? | Main limitation for TRACE’s needs | Direct implication for TRACE | How TRACE differs |
|---|---|---:|---|---|---|---|---|---|---|---|
| AgentHallu | Trace attribution benchmark | Yes | Hallucination attribution in LLM-agent trajectories | Multi-step agent trajectories across several frameworks and domains | Human-curated labels for hallucination presence, responsible step, and explanation; models are evaluated on localization and attribution quality | Yes. This is the core contribution: locate where hallucination occurred in the trajectory | Not primarily. It is an attribution benchmark, not an engineering repair/regression system | Strongly relevant to attribution, but narrower than TRACE: it does not by itself cover deterministic verifier correctness, repair effectiveness, overblocking, or regression reliability | Use AgentHallu as the closest methodological reference for exact-step accuracy, off-by-one accuracy, category accuracy, causal explanation, and judge-human agreement | TRACE should extend beyond hallucination attribution into task validity, deterministic verification, repair/regression workflows, and audit-ready reporting |
| AgentRx | Failure diagnosis / trace-forensics framework | Partial / adjacent | Diagnosing failed agent executions by identifying the critical failure step and classifying the failure | Failed execution trajectories from agent workflows | Critical-failure-step localization, failure taxonomy, constraint/evidence logs, and LLM-assisted diagnosis | Yes. It focuses on critical failure localization and failure categories | Limited / adjacent. It is stronger on diagnosis than full repair/regression lifecycle | Very close to TRACE’s attribution angle, but less clearly focused on validating task/verifier quality or operationalizing regression/audit metrics | Use AgentRx to sharpen TRACE’s definition of “first responsible step” vs “first visible failure” vs “critical unrecoverable step” | TRACE should differentiate by explicitly separating deterministic verifier results from LLM-assisted attribution and by adding repair effectiveness, overblocking, and regression reliability |
| WebArena | Realistic web-agent benchmark | Yes | Whether an autonomous web agent can complete realistic browser tasks | Long-horizon web tasks across simulated functional websites | Environment/task-specific success checks and final task completion metrics | Limited. It evaluates end-to-end task success more than step-level root cause | Not central | Excellent for realistic task design, but final success does not explain why the agent failed | Borrow its emphasis on realistic, reproducible environments and concrete task success criteria | TRACE should not compete as “another WebArena.” It should add verifier correctness and trace-level failure diagnosis on top of verifiable tasks |
| SWE-bench | Coding-agent benchmark | Yes | Whether an LLM/agent can resolve real GitHub issues | Software issues, repositories, patches, tests | Deterministic execution-based checks: generated patch must pass relevant tests | Limited. Usually focuses on final patch correctness rather than trajectory attribution | Indirectly. Tests naturally support regression thinking, but the benchmark is not mainly a repair-diagnosis workflow | Strong deterministic verification model, but test pass/fail alone may not explain failure cause; tests can be incomplete or flaky | Borrow executable verifier discipline, reproducible harnesses, and false-positive/false-negative thinking for verifiers | TRACE should evaluate the verifier itself and connect failed runs to attribution, repair recommendations, and regression checks |
| GAIA | General assistant benchmark | Yes | Whether a general AI assistant can answer real-world questions requiring reasoning, tools, search, and sometimes multimodal input | Real-world assistant questions | Final-answer correctness, often with constrained expected answers | Limited / not central | No | Useful for broad capability evaluation, but not focused on trace forensics, deterministic verifier design, or repair loops | Use GAIA as a contrast case: broad assistant success is different from trace-grounded reliability evaluation | TRACE should avoid claiming broad general-assistant coverage; instead, claim depth on verifiable workflows and failure analysis |
| AgentBench | Multi-environment agent benchmark suite | Yes | LLM-as-agent performance across multiple interactive environments | Several interactive environments testing reasoning, decisions, and long-horizon behavior | Environment-specific task scores | Limited / not central | No | Broad benchmark suite, but less actionable for debugging a specific failed trajectory | Use as background for the benchmark landscape and to show that many agent benchmarks emphasize aggregate success | TRACE should be narrower but deeper: not only “how often did the agent succeed?” but “was the task valid, was the verifier correct, and where did failure originate?” |
| Observability / tracing systems, e.g. LangSmith, Langfuse, OpenTelemetry-style tracing, Phoenix, Weave, Braintrust | Infrastructure, not benchmark | No, not primarily | Runtime behavior: traces, spans, tool calls, prompts, outputs, latency, cost, errors | Production/development traces from LLM and agent apps | Logging, tracing, dashboards, eval hooks, human review, and monitoring | They collect traces, but usually do not define rigorous attribution metrics by default | Some platforms support evals/regression tests, but usually as tooling rather than a benchmark methodology | Infrastructure is necessary but insufficient: a trace viewer shows what happened, not necessarily what was correct, valid, or causally responsible | Use span/trace concepts for TRACE’s data model: run id, step id, parent id, tool call, observation, verifier evidence, timestamps | TRACE should be positioned as an evaluation methodology/harness built on trace data, not merely an observability dashboard |
| Benchmark-validity literature | Methodology, not system | No | Whether benchmarks measure what they claim to measure | Benchmark/task design, construct validity, task validity, contamination, reliability, reproducibility, human agreement | Conceptual/statistical criteria, documentation standards, validity arguments, benchmark cards | Not usually | Not usually | Does not provide an implementation, but gives the language for TRACE’s claims and limitations | Use it to define task validity, verifier reliability, judge-human agreement, intended use, non-use, limitations, and non-claims | TRACE should explicitly document what its metrics do and do not prove, avoiding vague “quality scores” or unsupported benchmark-superiority claims |

## What TRACE must prove

TRACE should not claim to be globally “better” than existing agent benchmarks. It should prove that it adds a missing reliability layer:

1. Task validity: tasks are well-specified, solvable, reproducible, and objectively verifiable.
2. Verifier correctness: deterministic verifiers have measured false-positive and false-negative behavior.
3. Separation of evaluator types: deterministic pass/fail is reported separately from LLM-assisted attribution.
4. Trace completeness: logs contain enough step-level evidence to support failure diagnosis.
5. Attribution accuracy: TRACE can identify exact failure step, near-miss/off-by-one step, and failure category with measured human agreement.
6. Repair effectiveness: suggested fixes actually address the observed failure.
7. Overblocking: verifiers/guards do not unnecessarily reject valid behavior.
8. Regression reliability: fixes remain stable across repeated or related tasks.
9. Audit readiness: reports can be used by reviewers to understand decisions and limitations.

## Recommendations for TRACE design

### Verifier design

- Start with deterministic checks before judge-based scoring.
- For tool/API tasks, validate tool name, arguments, required sequence, final API/database state, forbidden actions, and schema compliance.
- For RAG tasks, validate source presence, source-span support, citation correctness, and unsupported/contradicted claims.
- Track verifier false positives and false negatives using seeded positive/negative cases.
- Store verifier evidence in structured form so the dashboard/audit report can explain every pass/fail.

### Attribution design

- Separate “deterministic failure” from “LLM-assisted explanation of the failure.”
- Label first responsible step, visible failure step, and critical unrecoverable step separately if they differ.
- Use exact-step accuracy, off-by-one accuracy, and category accuracy.
- Require judge explanations to cite specific trace evidence.
- Start with an AgentHallu-like taxonomy, then adapt it to TRACE tasks: Planning, Retrieval, Reasoning, Human-Interaction, Tool-Use, plus any TRACE-specific verifier/schema categories.

### Repair and regression design

- Every failure card should propose one of: task fix, verifier fix, agent fix, guardrail fix, or “no fix / expected failure.”
- Measure whether repairs fix the original failure without introducing regressions.
- Track overblocking: cases where a guard/verifier rejects valid agent behavior.
- Create regression tasks from repaired failures.

### Task-validity design

- Every task should include: goal, initial state, allowed tools/sources, expected outcome, deterministic verifier, known invalid cases, and reproducibility notes.
- Reject tasks that are ambiguous, impossible, unsafe, duplicate, or not objectively verifiable.
- Version task specs and verifiers so changes are auditable.

## Suggested final summary paragraph

TRACE differs from existing agent benchmarks by focusing on the reliability layer between raw traces and leaderboard scores. WebArena, SWE-bench, GAIA, and AgentBench primarily ask whether an agent completed a task. AgentHallu and AgentRx move closer to TRACE by asking where a trajectory went wrong, but they do not fully operationalize deterministic verifier evaluation, repair effectiveness, overblocking, regression reliability, and audit reporting as one workflow. TRACE’s Month 1 contribution should therefore be a compact methodology and matrix showing how valid tasks, measured verifiers, trace-level attribution, and regression-ready outputs fit together.
