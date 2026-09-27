"""method name -> attribution method. The only place a name becomes a class.

Mirrors ``verifiers/registry.py`` and ``environment/controls.py``: an unknown
name fails here, at selection time, with the available names in the message,
rather than somewhere downstream.
"""

from __future__ import annotations

from trace_harness.attribution.base import AttributionMethod, stamp_method_metadata
from trace_harness.attribution.heuristic import HeuristicAttributor
from trace_harness.attribution.schemas import AttributionResult
from trace_harness.runner.result import RunResult
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing.events import TraceEvent
from trace_harness.verifiers.base import VerifierResult

DEFAULT_METHOD = "heuristic"


class UnknownAttributionMethodError(ValueError):
    """A method name that is not registered."""


class _HeuristicMethod:
    """The rule-based baseline, wrapped as a method.

    Free and deterministic, which is what lets it run on every failure in CI.
    A judge will be neither, and that difference is the reason both numbers are
    recorded rather than assumed.
    """

    name = DEFAULT_METHOD
    deterministic = True

    def attribute(
        self,
        task: TaskSpec,
        trace: list[TraceEvent],
        verifier_result: VerifierResult,
        run_result: RunResult | None = None,
    ) -> AttributionResult:
        return HeuristicAttributor().attribute(task, trace, verifier_result, run_result)


_METHODS: dict[str, AttributionMethod] = {
    DEFAULT_METHOD: _HeuristicMethod(),
}


def available_methods() -> list[str]:
    return sorted(_METHODS)


def get_attribution_method(name: str) -> AttributionMethod:
    """Return the method registered under ``name`` or raise with the options."""
    try:
        return _METHODS[name]
    except KeyError:
        raise UnknownAttributionMethodError(
            f"unknown attribution method {name!r}; available: {available_methods()}"
        ) from None


def run_attribution(
    name: str,
    task: TaskSpec,
    trace: list[TraceEvent],
    verifier_result: VerifierResult,
    run_result: RunResult | None = None,
) -> AttributionResult:
    """Attribute through the registry, stamping method metadata onto the result.

    Cost is measured by the method itself where it has one; the heuristic is
    free, so it reports 0.0 rather than leaving the field absent. A missing
    cost and a zero cost mean different things once a judge exists.
    """
    method = get_attribution_method(name)
    result = method.attribute(task, trace, verifier_result, run_result)
    cost = float(getattr(method, "last_cost_usd", 0.0))
    return stamp_method_metadata(result, method, cost_usd=cost)
