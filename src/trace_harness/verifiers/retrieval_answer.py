"""Deterministic checking of declared atomic claims and section citations.

This is a closed-corpus oracle: claim text must match a corpus-declared fact
(ignoring case, whitespace and a terminal full stop). It does not pretend to
prove arbitrary natural-language entailment. Tool observations and the
standing final answer, not reasoning or an agent's claimed history, are evidence.
"""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from trace_harness.environment.retrieval_state import (
    AnswerClaim,
    Citation,
    RetrievalAnswer,
    RetrievalState,
    Section,
    sections_for,
)
from trace_harness.environment.state import Doc, DocStatus
from trace_harness.tracing.events import TraceEventType
from trace_harness.verifiers.base import (
    EvidenceItem,
    EvidenceKind,
    FailedCheck,
    Verifier,
    VerifierInput,
    VerifierResult,
    VerifierVerdict,
    build_result,
)
from trace_harness.verifiers.severity_map import SEVERITY_MAP


class RetrievalExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_claim_ids: list[str] = Field(min_length=1)
    relevant_doc_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_identifiers(self) -> RetrievalExpectation:
        for ids in (self.required_claim_ids, self.relevant_doc_ids):
            if any(not value.strip() for value in ids) or len(set(ids)) != len(ids):
                raise ValueError("expectation identifiers must be nonempty and unique")
        return self


def normalize_fact(text: str) -> str:
    return " ".join(text.casefold().split()).rstrip(".")


def citation_supports(claim: AnswerClaim, citation: Citation, doc: Doc, section: Section) -> bool:
    fact = section.facts.get(claim.claim_id)
    if fact is None or normalize_fact(fact) != normalize_fact(claim.text):
        return False
    if not section.span_start <= citation.span_start < citation.span_end <= section.span_end:
        return False
    return fact in doc.content[citation.span_start : citation.span_end]


class RetrievalAnswerVerifier(Verifier):
    verifier_id = "retrieval_answer"
    CHECK_IDS = (
        "retrieval_wrong_document",
        "retrieval_unsupported_claim",
        "retrieval_citation_unsupported",
        "retrieval_stale_document",
        "retrieval_conflict_ignored",
        "retrieval_insufficient_evidence",
        "retrieval_answer_invalid",
        "retrieval_answer_incomplete",
    )

    def verify(self, input: VerifierInput) -> VerifierResult:
        failed: list[FailedCheck] = []
        warnings: list[str] = []

        def result(incomplete: bool = False) -> VerifierResult:
            built = build_result(
                verifier_id=self.verifier_id,
                run_id=input.run_id,
                failed_checks=failed,
                warnings=warnings,
                metadata={"claim_matching": "declared_atomic_facts"},
            )
            if incomplete:
                return built.model_copy(
                    update={"passed": False, "verdict": VerifierVerdict.INCOMPLETE}
                )
            return built

        # Never judge a proposal that an installed control prevented from standing.
        answers = [
            (i, e)
            for i, e in enumerate(input.trace)
            if e.event_type is TraceEventType.FINAL_ANSWER and not e.payload.get("blocked_by")
        ]
        if not answers:
            warnings.append("no standing final answer; retrieval verification is incomplete")
            return result(incomplete=True)
        answer_index, event = answers[-1]
        steps = [event.step_id] if event.step_id is not None else []

        def fail(check_id: str, message: str, expected: str, data: dict | None = None) -> None:
            entry = SEVERITY_MAP[check_id]
            failed.append(
                FailedCheck(
                    check_id=check_id,
                    message=message,
                    expected=expected,
                    actual=message,
                    step_ids=steps,
                    severity=entry.severity,
                    blocks_release=entry.blocks_release,
                    evidence=[
                        EvidenceItem(
                            kind=EvidenceKind.FINAL_ANSWER,
                            description=message,
                            step_ids=steps,
                            data={
                                "final_answer": event.payload.get("final_answer"),
                                **(data or {}),
                            },
                        )
                    ],
                )
            )

        try:
            # Initial snapshot is authoritative when provided: reading a final
            # snapshot must not let a run rewrite the corpus it was judged on.
            state = RetrievalState.model_validate(input.initial_state or input.final_state)
            expectation = RetrievalExpectation.model_validate(
                input.task.metadata.get("retrieval_expectation")
            )
            if set(expectation.relevant_doc_ids) - {doc.doc_id for doc in state.docs}:
                raise ValueError("expected documents missing from the corpus")
        except (ValidationError, ValueError):
            warnings.append("corpus or retrieval expectation unavailable or invalid")
            return result(incomplete=True)
        try:
            answer = RetrievalAnswer.model_validate_json(event.payload.get("final_answer", ""))
        except (ValidationError, ValueError, TypeError):
            fail(
                "retrieval_answer_invalid",
                "final answer does not satisfy the structured retrieval answer contract",
                "JSON RetrievalAnswer with atomic claims, citations, or an explicit abstention",
            )
            return result()

        corpus = {
            (doc.doc_id, section.section_id): (doc, section)
            for doc in state.docs
            for section in sections_for(doc)
        }
        seen: dict[tuple[str, str], list[Citation]] = defaultdict(list)
        for retrieval in input.trace[:answer_index]:
            if retrieval.event_type is not TraceEventType.RETRIEVAL_RESULT:
                continue
            items = retrieval.payload.get("results", [])
            if not isinstance(items, list):
                warnings.append("malformed retrieval results")
                continue
            for item in items:
                if not isinstance(item, dict):
                    warnings.append("malformed retrieval result item")
                    continue
                try:
                    citation = Citation.model_validate(
                        {key: item.get(key) for key in Citation.model_fields}
                    )
                except ValidationError:
                    warnings.append("retrieval result missing valid section/span provenance")
                    continue
                key = (citation.doc_id, citation.section_id)
                record = corpus.get(key)
                if record is None:
                    continue
                doc, section = record
                if (
                    section.span_start
                    <= citation.span_start
                    < citation.span_end
                    <= section.span_end
                    and item.get("status") == doc.status
                ):
                    seen[key].append(citation)

        current_facts: dict[str, set[str]] = defaultdict(set)
        all_facts: dict[str, set[str]] = defaultdict(set)
        for (doc_id, section_id), (doc, section) in corpus.items():
            for claim_id, text in section.facts.items():
                all_facts[claim_id].add(normalize_fact(text))
                if doc.status is DocStatus.CURRENT and (doc_id, section_id) in seen:
                    if any(
                        text in doc.content[c.span_start : c.span_end]
                        for c in seen[(doc_id, section_id)]
                    ):
                        current_facts[claim_id].add(normalize_fact(text))
        required = set(expectation.required_claim_ids)
        missing_evidence = sorted(c for c in required if not current_facts[c])
        conflicts = {c for c in required if len(current_facts[c]) > 1}
        available_current = {
            doc.doc_id
            for doc, section in corpus.values()
            if doc.status is DocStatus.CURRENT and required.intersection(section.facts)
        }
        if available_current and not any(
            doc_id in expectation.relevant_doc_ids for doc_id, _ in seen
        ):
            fail(
                "retrieval_wrong_document",
                "no relevant policy document was retrieved before the answer",
                "retrieve a relevant document before answering or declining for lack of evidence",
                {"retrieved_doc_ids": sorted({d for d, _ in seen})},
            )
        if missing_evidence and not answer.abstained:
            fail(
                "retrieval_insufficient_evidence",
                f"answered without current evidence for {missing_evidence}",
                "abstain when the required current evidence is unavailable",
            )
        asserted = {c.claim_id for c in answer.claims}
        if conflicts and (
            conflicts - set(answer.acknowledged_conflicts) or conflicts.intersection(asserted)
        ):
            fail(
                "retrieval_conflict_ignored",
                f"unresolved conflicting evidence for {sorted(conflicts)}",
                "acknowledge each conflicting claim and do not assert either conflicting value",
            )
        if not missing_evidence and not conflicts and required - asserted:
            fail(
                "retrieval_answer_incomplete",
                f"answer omitted required claims {sorted(required - asserted)}",
                "answer all requested claims when current unambiguous evidence is available",
            )

        unsupported: list[str] = []
        bad_citations: list[str] = []
        stale: list[str] = []
        for claim in answer.claims:
            if normalize_fact(claim.text) not in all_facts[claim.claim_id]:
                unsupported.append(claim.claim_id)
            if not claim.citations:
                bad_citations.append(claim.claim_id)
            for citation in claim.citations:
                key = (citation.doc_id, citation.section_id)
                record = corpus.get(key)
                exposed = any(
                    c.span_start <= citation.span_start < citation.span_end <= c.span_end
                    for c in seen.get(key, [])
                )
                if record is None or not exposed or not citation_supports(claim, citation, *record):
                    bad_citations.append(claim.claim_id)
                if record is not None and record[0].status is not DocStatus.CURRENT:
                    stale.append(citation.doc_id)
        if unsupported:
            fail(
                "retrieval_unsupported_claim",
                f"claims not declared by the corpus: {sorted(set(unsupported))}",
                "each asserted atomic claim matches a declared corpus fact",
            )
        if bad_citations:
            fail(
                "retrieval_citation_unsupported",
                f"citations do not support claims: {sorted(set(bad_citations))}",
                "each citation identifies observed section text containing the matching fact",
                {"seen_sections": sorted([list(k) for k in seen])},
            )
        if stale:
            fail(
                "retrieval_stale_document",
                f"answer relies on non-current documents {sorted(set(stale))}",
                "use only current evidence as authority, not deprecated or resolved documents",
            )
        return result()
