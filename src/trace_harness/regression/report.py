"""Structured observations from the existing replay command for CI consumers."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ReplayCaseResult(BaseModel):
    test_name: str
    run_id: str
    completed: bool
    verifier_passed: bool
    failed_checks: list[str]
    blocking_checks: list[str]

    @property
    def passed(self) -> bool:
        return self.completed and self.verifier_passed


class ReplayReport(BaseModel):
    """Keep legacy exit status separate from the collector's completion checks.

    The collector must not accept a partial run simply because it happened to
    emit the expected violation before terminating. Existing replay exit codes
    are preserved; consumers can require completed evidence using these fields.
    """

    exit_code: int
    expected_checks: list[str]
    scenario: ReplayCaseResult
    siblings: list[ReplayCaseResult] = Field(default_factory=list)

    @property
    def reproduced(self) -> bool:
        return (
            self.scenario.completed
            and not self.scenario.verifier_passed
            and bool(self.expected_checks)
            and set(self.expected_checks) <= set(self.scenario.failed_checks)
        )

    @property
    def control_confirmed(self) -> bool:
        return (
            self.scenario.completed
            and not set(self.expected_checks) & set(self.scenario.failed_checks)
            and not self.scenario.blocking_checks
            and all(sibling.passed for sibling in self.siblings)
        )
