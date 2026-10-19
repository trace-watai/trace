"""Section corpus and answer contracts for deterministic, bounded claim checking.

Spans are zero-based, half-open Python character offsets in ``Doc.content``.
Facts are authored corpus data, not an entailment model. The verifier supports
only those declared atomic claims, and makes no open-ended semantic claim.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trace_harness.environment.state import Doc
from trace_harness.tasks.schemas import TaskSpec

RETRIEVAL_STATE_SCHEMA_VERSION = "0.1.0"
RETRIEVAL_ANSWER_SCHEMA_VERSION = "0.1.0"


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    section_id: str = Field(min_length=1)
    title: str
    span_start: int = Field(ge=0)
    span_end: int = Field(gt=0)
    facts: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ordered_span(self) -> Section:
        if self.span_end <= self.span_start:
            raise ValueError("section span must be nonempty and ordered")
        return self


def sections_for(doc: Doc) -> list[Section]:
    raw_sections = doc.metadata.get("sections", [])
    if not isinstance(raw_sections, list):
        raise ValueError(f"{doc.doc_id}: sections must be a list")
    sections = [Section.model_validate(s) for s in raw_sections]
    if len({s.section_id for s in sections}) != len(sections):
        raise ValueError(f"{doc.doc_id}: duplicate section ids")
    previous_end = 0
    for section in sorted(sections, key=lambda s: s.span_start):
        if section.span_start < previous_end or section.span_end > len(doc.content):
            raise ValueError(f"{doc.doc_id}: overlapping or out-of-bounds section span")
        text = doc.content[section.span_start : section.span_end]
        if any(not claim_id.strip() for claim_id in section.facts):
            raise ValueError(f"{doc.doc_id}: fact claim ids must be nonempty")
        if any(not fact.strip() or fact not in text for fact in section.facts.values()):
            raise ValueError(f"{doc.doc_id}: declared facts must occur in their section")
        previous_end = section.span_end
    return sections


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    doc_id: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    span_start: int = Field(ge=0)
    span_end: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered_span(self) -> Citation:
        if self.span_end <= self.span_start:
            raise ValueError("citation span must be nonempty and ordered")
        return self


class AnswerClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    citations: list[Citation] = Field(default_factory=list)


class RetrievalAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: str = RETRIEVAL_ANSWER_SCHEMA_VERSION
    claims: list[AnswerClaim] = Field(default_factory=list)
    abstained: bool = False
    acknowledged_conflicts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_answer(self) -> RetrievalAnswer:
        if self.schema_version != RETRIEVAL_ANSWER_SCHEMA_VERSION:
            raise ValueError("unsupported retrieval answer schema")
        if self.abstained and self.claims:
            raise ValueError("an abstention cannot also assert claims")
        if not self.abstained and not self.claims:
            raise ValueError("an answer must assert a claim or abstain")
        ids = [c.claim_id for c in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("claim ids must be unique")
        return self


class SeenSection(Citation):
    retrieved_at_step: int | None = None


class RetrievalState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = RETRIEVAL_STATE_SCHEMA_VERSION
    docs: list[Doc] = Field(default_factory=list)
    seen_sections: list[SeenSection] = Field(default_factory=list)
    doc_ranking_override: list[str] | None = None

    @model_validator(mode="after")
    def valid_corpus(self) -> RetrievalState:
        if len({d.doc_id for d in self.docs}) != len(self.docs):
            raise ValueError("duplicate document ids")
        for doc in self.docs:
            if not sections_for(doc):
                raise ValueError(f"{doc.doc_id}: retrieval documents must declare sections")
        return self

    @classmethod
    def from_task(cls, task: TaskSpec, docs: list[Doc] | None = None) -> RetrievalState:
        payload = dict(task.initial_state)
        payload["docs"] = list(docs or []) + list(payload.get("docs", []))
        return cls.model_validate(payload)

    def snapshot(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
