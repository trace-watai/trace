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

Intended evolution
    Chunking (today: one doc == one chunk), stemming/synonyms, and pluggable
    scorers — behind this same function signature so the tool layer doesn't
    change.

# TODO(Evan Yang/environment): add a fixture-pinned ranking override
# (task-supplied doc_id order) for adversarial retrieval scenarios where
# keyword scoring is too well-behaved.
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
) -> list[RetrievedChunk]:
    """Score ``docs`` against ``query`` and return the top matches, best first."""
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
    scored.sort(key=lambda c: (-c.score, c.doc_id))
    return scored[:top_k]
