"""RunResult: how a run ended, separate from whether it was *correct*.

Crucial distinction: ``status=completed`` means the agent produced a final
answer within limits — it says nothing about correctness. Correctness is
the verifier's job, recorded in ``verifier_result.json``. A run that issues
a catastrophic unauthorized refund and then politely answers the customer
is ``completed``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

RUN_RESULT_SCHEMA_VERSION = "0.1.0"


class RunStatus(StrEnum):
    COMPLETED = "completed"  # reached a final answer
    TERMINATED = "terminated"  # stopped by harness limits (steps/timeout/script end)
    ERROR = "error"  # adapter or harness exception


class TerminationReason(StrEnum):
    FINAL_ANSWER = "final_answer"
    MAX_STEPS_REACHED = "max_steps_reached"
    TIMEOUT = "timeout"
    SCRIPT_EXHAUSTED = "script_exhausted"
    MODEL_ERROR = "model_error"
    INTERNAL_ERROR = "internal_error"


class RunResult(BaseModel):
    schema_version: str = RUN_RESULT_SCHEMA_VERSION
    run_id: str
    task_id: str
    status: RunStatus
    termination_reason: TerminationReason
    steps_taken: int
    final_output: str | None = None
    # Artifact name -> path relative to the runs directory, for API/dashboard use.
    artifact_paths: dict[str, str] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime
    error: str | None = None
