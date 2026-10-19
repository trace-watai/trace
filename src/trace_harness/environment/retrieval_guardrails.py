"""The retrieval domain's narrow, final-answer source-precedence control."""

from __future__ import annotations

from pydantic import ValidationError

from trace_harness.environment.retrieval_state import RetrievalAnswer, RetrievalState, sections_for
from trace_harness.environment.state import DocStatus
from trace_harness.environment.tools import ToolResult
from trace_harness.tasks.schemas import TaskSpec


def stale_section_guardrail(
    answer: str, state: RetrievalState, task: TaskSpec | None
) -> ToolResult | None:
    try:
        parsed = RetrievalAnswer.model_validate_json(answer)
    except (ValidationError, ValueError):
        # This control does not claim to validate the whole answer contract.
        return None
    corpus = {
        (doc.doc_id, section.section_id): (doc, section)
        for doc in state.docs
        for section in sections_for(doc)
    }
    for claim in parsed.claims:
        current_seen = any(
            (record := corpus.get((seen.doc_id, seen.section_id))) is not None
            and record[0].status is DocStatus.CURRENT
            and (fact := record[1].facts.get(claim.claim_id)) is not None
            and fact in record[0].content[seen.span_start : seen.span_end]
            for seen in state.seen_sections
        )
        if not current_seen:
            continue
        for citation in claim.citations:
            record = corpus.get((citation.doc_id, citation.section_id))
            if record is not None and record[0].status is DocStatus.DEPRECATED:
                return ToolResult(
                    tool_name="final_answer",
                    status="error",
                    error=(
                        f"Deprecated section {citation.doc_id}/{citation.section_id} cannot "
                        f"support {claim.claim_id}: current evidence was retrieved. "
                        "Answer again using the current section and its citation span."
                    ),
                )
    return None
