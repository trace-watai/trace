# ADR-0001: Initial architecture — fixture-first, deterministic-first, local-first

**Status:** Accepted (2026-06).

## Context

TRACE's first commit must let ~13 people work in parallel on an agent
reliability harness before any real model integration, budget, or
infrastructure exists. The riskiest failure modes at this stage are: (a)
building evaluation machinery whose own behavior is unreproducible, (b)
coupling everything to one model provider or one scenario, (c) burning
weeks on infrastructure nobody's work actually needs yet, and (d) a repo
where nobody knows where their work goes.

## Decision

1. **Fixture-first execution.** The default target agent is a scripted
   `FixtureModelAdapter` replaying authored action sequences. Real model
   adapters implement the same `ModelAdapter` protocol (a Gemini scaffold
   pins the contract). Tests and CI never require API keys or network.
2. **Pydantic v2 schemas as contracts.** Every boundary object (task,
   trace event, run result, verifier result, attribution, card, repair,
   regression) is a typed, versioned model. `extra="forbid"` on fixtures
   so typos fail at load time.
3. **Local JSON/JSONL artifacts.** One directory per run with documented
   filenames (`tracing/artifact_store.py` is the single source of truth).
   Pipeline stages communicate only through these files. No database, no
   object store.
4. **Mock support environment.** A typed in-memory world (orders, refunds,
   tickets, docs) with four tools carrying declared side-effect classes,
   and deterministic keyword retrieval. No vector DB.
5. **Deterministic verifier first.** Pass/fail comes from rule-based
   checks over recorded state and trace, with policy rules read as data
   from the pinned current-policy doc. LLM judges arrive later, for
   explanation — never for the verdict (see verifier_philosophy.md).
6. **Heuristic attribution before LLM attribution.** A rule-based
   attributor establishes the schema, the step vocabulary (root cause ≠
   first irreversible), and the baseline an LLM judge must beat.
7. **No hosted services initially.** No Docker/K8s/cloud, no API server,
   no dashboard implementation — placeholder docs define their contracts
   and start conditions instead.

## Why these over the alternatives

- *Live-model-first* (rejected): non-deterministic from day one, so
  verifier/attribution/dashboard work would be debugged against moving
  targets, and CI would cost money and flake. The fixture agent gives
  byte-stable traces (tested) that make every downstream component
  testable now.
- *Plain dicts / JSON Schema only* (rejected): dict-shaped contracts rot
  silently; Pydantic gives validation, IDE types, and JSON Schema for free
  at the one cost of a single dependency.
- *Database-backed storage now* (rejected): a runs directory is
  greppable, diffable, trivially consumed by a static dashboard, and
  survives total tooling loss. A backend earns its way in when two
  consumers need the same queries (docs/future_api.md states the
  trigger).
- *Vector retrieval now* (rejected): the first scenarios need
  *controllable* retrieval order to stage failures; keyword scoring with
  documented tie-breaks delivers that. Embeddings add a dependency and
  nondeterminism with zero current benefit.
- *LLM judge now* (rejected): without a deterministic baseline and a
  labeled set, a judge's output is unfalsifiable. Sequencing it second
  makes it measurable.

## Consequences

**Positive:** parallel workstreams with crisp seams; sub-second offline CI
with no API keys; every artifact human-readable; reproducibility as a
tested property; provider-agnostic from the first commit.

**Costs we accept:**
- Scripted failures are staged, not discovered — fixture results say
  nothing about real model quality until live adapters land.
- Keyword retrieval and keyword claim-matching are honest but crude;
  structured citations and better matchers are named next steps.
- Local-only artifacts mean no shared run history across machines yet.
- Some MVP shortcuts (generic event payloads, merge-based multi-verifier,
  replay-via-fixture) are documented debts with named owners rather than
  solved problems.

**Revisit triggers:** first live adapter (runner timeout/retry design),
dashboard's first read pass (typed event payloads), >1 workflow
(environment generalization), shared-team run storage (API/backend).
