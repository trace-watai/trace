"""Read-only policy retrieval with stable section ids and citation spans."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from trace_harness.environment.registry import ToolRegistry
from trace_harness.environment.retrieval import RetrievedChunk, search_docs
from trace_harness.environment.retrieval_state import RetrievalState, SeenSection, sections_for
from trace_harness.environment.sandbox import SandboxEnvironment
from trace_harness.environment.state import Doc, DocStatus
from trace_harness.environment.tools import ToolDefinition, ToolResult, ToolSideEffect
from trace_harness.tasks.schemas import TaskSpec


class SearchSectionsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    status_filter: DocStatus | None = None
    top_k: int = Field(default=5, ge=1, le=20)


class ReadSectionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str
    section_id: str


def _sections(state: RetrievalState) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            doc_id=doc.doc_id,
            section_id=section.section_id,
            title=f"{doc.title}: {section.title}",
            status=doc.status,
            source=doc.source,
            content=doc.content[section.span_start : section.span_end],
            span_start=section.span_start,
            span_end=section.span_end,
            score=0.0,
        )
        for doc in state.docs
        for section in sections_for(doc)
    ]


def _result(
    state: RetrievalState,
    tool_name: str,
    chunks: list[RetrievedChunk],
    step_id: int | None,
    query: str | None = None,
) -> ToolResult:
    state.seen_sections.extend(
        SeenSection(
            doc_id=c.doc_id,
            section_id=c.section_id,
            span_start=c.span_start,
            span_end=c.span_end,
            retrieved_at_step=step_id,
        )
        for c in chunks
    )
    return ToolResult(
        tool_name=tool_name,
        status="ok",
        result={
            "query": query,
            "result_count": len(chunks),
            "results": [c.model_dump(mode="json") for c in chunks],
        },
        retrieval=chunks,
    )


def _search(state: RetrievalState, args: BaseModel, step_id: int | None) -> ToolResult:
    assert isinstance(args, SearchSectionsArgs)
    # Reuse keyword scoring without conflating a section's identity with its
    # parent document's. The temporary ids are never exposed to the agent.
    chunks = _sections(state)
    candidates = [
        Doc(
            doc_id=str(i),
            title=c.title,
            status=c.status,
            content=c.content,
            source=c.source,
        )
        for i, c in enumerate(chunks)
    ]
    hits = search_docs(candidates, args.query, status_filter=args.status_filter, top_k=len(chunks))
    ranked = [chunks[int(h.doc_id)].model_copy(update={"score": h.score}) for h in hits]
    order = {doc_id: i for i, doc_id in enumerate(state.doc_ranking_override or [])}
    ranked.sort(
        key=lambda c: (
            order.get(c.doc_id, len(order)),
            -c.score,
            c.doc_id,
            c.section_id,
        )
    )
    return _result(state, "search_docs", ranked[: args.top_k], step_id, args.query)


def _read(state: RetrievalState, args: BaseModel, step_id: int | None) -> ToolResult:
    assert isinstance(args, ReadSectionArgs)
    chunks = [
        c for c in _sections(state) if (c.doc_id, c.section_id) == (args.doc_id, args.section_id)
    ]
    if not chunks:
        return ToolResult(
            tool_name="read_section", status="error", error="unknown document/section"
        )
    return _result(state, "read_section", chunks, step_id)


def retrieval_registry() -> ToolRegistry[RetrievalState]:
    return ToolRegistry(
        [
            ToolDefinition(
                name="search_docs",
                description=(
                    "Search policy sections. Returns full section text, document status and "
                    "zero-based half-open character spans into the parent document."
                ),
                args_model=SearchSectionsArgs,
                side_effect=ToolSideEffect.READ_ONLY,
                handler=_search,
            ),
            ToolDefinition(
                name="read_section",
                description="Read a section by its stable document and section ids.",
                args_model=ReadSectionArgs,
                side_effect=ToolSideEffect.READ_ONLY,
                handler=_read,
            ),
        ]
    )


class RetrievalEnvironment(SandboxEnvironment[RetrievalState]):
    workflow_type = "retrieval.answer"

    @classmethod
    def from_task(
        cls,
        task: TaskSpec,
        docs: list[Doc] | None = None,
        control_library: Path | str | None = None,
    ) -> RetrievalEnvironment:
        return cls(
            RetrievalState.from_task(task, docs),
            retrieval_registry(),
            available_tools=task.available_tools,
            control_library=control_library,
            task=task,
        )
