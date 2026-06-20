"""Deterministic keyword retrieval over the environment's document corpus.

Why deterministic
    The first vertical slice needs retrieval whose *order and content are
    controllable from fixtures*, so failure scenarios (deprecated doc ranked
    alongside current doc) reproduce byte-for-byte in tests and CI. No
    embeddings, no vector DB, no network.

How scoring works (MVP)
    Tokenize query/title/content to lowercase alphanumeric tokens. Each
    distinct query token scores 2.0 if present in the title and 1.0 if
    present in the content. Ties break by ``doc_id`` ascending, so ordering
    is total and stable. Documents that match nothing are omitted.

Important guarantees
    - Content is returned in full — never truncated. Truncating policy text
      is exactly the kind of silent corruption TRACE exists to catch, so the
      harness must not do it itself.
    - Every chunk carries ``status`` metadata (current/deprecated/resolved);
      whether the *agent* respects it is the scenario under test.

Ranking override
    ``search_docs`` accepts an optional ``ranking_override`` — an ordered
    list of ``doc_id`` strings supplied by a fixture. When present, matching
    docs whose ``doc_id`` appears in the list are returned first (in list
    order), followed by remaining matches sorted by score descending then
    ``doc_id`` ascending. The relevance floor (score > 0) still applies;
    the override only changes ordering, never injects irrelevant results.

Intended evolution
    Chunking (today: one doc == one chunk), stemming/synonyms, and pluggable
    scorers — behind this same function signature so the tool layer doesn't
    change.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from trace_harness.environment.state import Doc, DocStatus

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


class RetrievedChunk(BaseModel):
    """One retrieval hit. MVP returns whole documents as single chunks."""

    doc_id: str
    status: DocStatus
    title: str
    content: str
    score: float
    source: str


def search_docs(
    docs: list[Doc],
    query: str,
    status_filter: DocStatus | None = None,
    top_k: int = 5,
    ranking_override: list[str] | None = None,
) -> list[RetrievedChunk]:
    """Score ``docs`` against ``query`` and return the top matches, best first.

    If ``ranking_override`` is provided it is treated as an ordered list of
    ``doc_id`` strings: matching docs whose id appears in the list come first
    (in list order), remaining matches follow sorted by score desc then
    ``doc_id`` asc. Docs with score ≤ 0 are always excluded regardless of
    whether they appear in the override.
    """
    query_tokens = _tokens(query)
    scored: list[RetrievedChunk] = []
    for doc in docs:
        if status_filter is not None and doc.status is not status_filter:
            continue
        title_tokens = _tokens(doc.title)
        content_tokens = _tokens(doc.content)
        score = sum(2.0 for t in query_tokens if t in title_tokens) + sum(
            1.0 for t in query_tokens if t in content_tokens
        )
        if score <= 0:
            continue
        scored.append(
            RetrievedChunk(
                doc_id=doc.doc_id,
                status=doc.status,
                title=doc.title,
                content=doc.content,
                score=score,
                source=doc.source,
            )
        )
    if ranking_override:
        order_index = {doc_id: i for i, doc_id in enumerate(ranking_override)}
        scored.sort(
            key=lambda c: (
                order_index.get(c.doc_id, len(ranking_override)),
                -c.score,
                c.doc_id,
            )
        )
    else:
        scored.sort(key=lambda c: (-c.score, c.doc_id))
    return scored[:top_k]
