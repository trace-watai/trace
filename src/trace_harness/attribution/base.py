"""The attribution method seam (#189).

Attribution used to mean one class. ``attribute`` constructed
``HeuristicAttributor`` directly, so there was nowhere to plug a judge in and
nothing to compare one against. The README's claim that "the judge has to beat
it" needs both a seam and a number, and this module is the seam.

An :class:`AttributionMethod` takes the same inputs (the task, the trace, the
verifier result and, when there is one, the run result) and returns the same
:class:`AttributionResult` whatever is behind it, so a heuristic and a judge are
directly comparable on identical runs. Each method records what it cost and
whether it is deterministic, because those are the two things that decide
whether a method can run in CI on every failure or only in a sweep.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from trace_harness.attribution.schemas import AttributionResult
from trace_harness.runner.result import RunResult
from trace_harness.tasks.schemas import TaskSpec
from trace_harness.tracing.events import TraceEvent
from trace_harness.verifiers.base import VerifierResult

# Metadata keys every method must set, so a result can be read without knowing
# which method produced it.
METHOD_NAME_KEY = "attribution_method"
COST_KEY = "cost_usd"
DETERMINISTIC_KEY = "deterministic"


@runtime_checkable
class AttributionMethod(Protocol):
    """Anything that can localize a failure from a run's evidence.

    Implementations must be side-effect free and must not mutate their inputs.
    A method that cannot localize anything returns a result with null step
    fields and an ambiguity note, rather than raising or guessing.
    """

    #: Registry key, also written into result metadata.
    name: str
    #: False for anything whose output can vary between identical inputs.
    deterministic: bool

    def attribute(
        self,
        task: TaskSpec,
        trace: list[TraceEvent],
        verifier_result: VerifierResult,
        run_result: RunResult | None = None,
    ) -> AttributionResult:
        """Localize the failure described by ``verifier_result``.

        ``run_result`` says how the run ended, which the post-block label reads
        (#157); without it the label falls back to the trace.
        """
        ...


def stamp_method_metadata(
    result: AttributionResult, method: AttributionMethod, *, cost_usd: float
) -> AttributionResult:
    """Record which method produced a result, what it cost, and its determinism.

    Stamped centrally rather than inside each method so the three keys cannot
    drift apart, and so a method author cannot forget one.
    """
    return result.model_copy(
        update={
            "metadata": {
                **result.metadata,
                METHOD_NAME_KEY: method.name,
                COST_KEY: cost_usd,
                DETERMINISTIC_KEY: method.deterministic,
            }
        }
    )
