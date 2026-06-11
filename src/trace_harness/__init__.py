"""TRACE agent reliability harness.

TRACE runs AI agents through realistic workflows, captures structured
execution traces, verifies whether the outcome was actually correct, and
turns failures into evidence-backed artifacts (failure cards, repair
packages, regression tests).

Canonical loop::

    task
    -> target-agent execution
    -> structured trace
    -> deterministic verifier
    -> attribution / judge
    -> failure card
    -> repair package
    -> regression test
    -> dashboard

Core principle: **the verifier decides pass/fail; the judge explains why.**

This ``__init__`` is intentionally minimal. Import from the submodules
directly (``trace_harness.tasks.schemas``, ``trace_harness.runner.agent_runner``,
...) so module boundaries stay visible and import cycles stay impossible.
See ``docs/architecture.md`` at the repo root for the module map.
"""

__version__ = "0.1.0"
