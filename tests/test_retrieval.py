"""Unit tests for deterministic keyword retrieval (retrieval.py).

All tests are offline and use in-memory Doc objects — no fixture files needed.
The function under test: search_docs(docs, query, status_filter, top_k, ranking_override).

Scoring contract:
    - Each query token present in the doc title   → +2.0 pts
    - Each query token present in the doc content → +1.0 pts
    - Docs with score ≤ 0 are always excluded
    - Ties break by doc_id ascending (lexicographic)
"""

from __future__ import annotations

import pytest

from trace_harness.environment.retrieval import RetrievedChunk, search_docs
from trace_harness.environment.state import Doc, DocStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc(
    doc_id: str,
    title: str = "",
    content: str = "",
    status: DocStatus = DocStatus.CURRENT,
) -> Doc:
    """Minimal Doc for unit tests; source defaults keep assertions simple."""
    return Doc(doc_id=doc_id, title=title, content=content, status=status, source="fixture")


# ---------------------------------------------------------------------------
# Scoring: title and content weights
# ---------------------------------------------------------------------------


def test_title_match_scores_two_points_per_token() -> None:
    docs = [_doc("a", title="refund policy")]
    results = search_docs(docs, "refund")
    assert len(results) == 1
    assert results[0].score == 2.0


def test_content_match_scores_one_point_per_token() -> None:
    docs = [_doc("a", title="guide", content="refund information")]
    results = search_docs(docs, "refund")
    assert len(results) == 1
    assert results[0].score == 1.0


def test_title_and_content_hit_accumulate() -> None:
    # "refund" in title (2) + "refund" in content (1) = 3, but tokens are sets
    # so each distinct query token scores once per location, not per occurrence.
    docs = [_doc("a", title="refund policy", content="refund window details")]
    results = search_docs(docs, "refund policy")
    # "refund": +2 (title) +1 (content) = 3; "policy": +2 (title) = 2; total = 5
    assert results[0].score == 5.0


def test_repeated_token_in_query_scores_once() -> None:
    # Query tokens are lowercased into a set; duplicates collapse.
    docs = [_doc("a", title="refund refund")]
    results = search_docs(docs, "refund refund")
    assert results[0].score == 2.0  # "refund" in title once = 2.0, not 4.0


def test_zero_score_docs_are_excluded() -> None:
    docs = [_doc("a", title="shipping info", content="tracking number")]
    results = search_docs(docs, "refund policy")
    assert results == []


def test_empty_corpus_returns_empty() -> None:
    assert search_docs([], "refund") == []


def test_empty_query_excludes_all_docs() -> None:
    # No query tokens → score 0 for every doc → all excluded.
    docs = [_doc("a", title="refund policy", content="cash window")]
    assert search_docs(docs, "") == []


# ---------------------------------------------------------------------------
# Result ordering: higher score first, tie-break by doc_id ascending
# ---------------------------------------------------------------------------


def test_higher_score_ranks_first() -> None:
    docs = [
        _doc("b", title="refund"),          # score 2 (title only)
        _doc("a", title="refund policy"),   # score 4 (two title tokens)
    ]
    results = search_docs(docs, "refund policy")
    assert [c.doc_id for c in results] == ["a", "b"]


def test_tie_breaks_by_doc_id_ascending() -> None:
    docs = [
        _doc("z_doc", title="refund"),
        _doc("a_doc", title="refund"),
    ]
    results = search_docs(docs, "refund")
    assert results[0].score == results[1].score  # confirm it's a tie
    assert [c.doc_id for c in results] == ["a_doc", "z_doc"]


def test_determinism() -> None:
    docs = [
        _doc("b-doc", title="refund policy", content="cash refund"),
        _doc("a-doc", title="cash", content="refund policy"),
    ]
    first = search_docs(docs, "refund policy cash")
    second = search_docs(docs, "refund policy cash")
    assert [r.doc_id for r in first] == [r.doc_id for r in second]
    assert [r.score for r in first] == [r.score for r in second]


# ---------------------------------------------------------------------------
# top_k
# ---------------------------------------------------------------------------


def test_top_k_limits_result_count() -> None:
    docs = [_doc(f"doc_{i}", title="refund") for i in range(10)]
    results = search_docs(docs, "refund", top_k=3)
    assert len(results) == 3


def test_top_k_default_is_five() -> None:
    docs = [_doc(f"doc_{i}", title="refund") for i in range(8)]
    results = search_docs(docs, "refund")
    assert len(results) == 5


def test_top_k_larger_than_corpus_returns_all_matches() -> None:
    docs = [_doc("a", title="refund"), _doc("b", title="shipping")]
    results = search_docs(docs, "refund", top_k=20)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# status_filter
# ---------------------------------------------------------------------------


def test_status_filter_current_excludes_deprecated_and_resolved() -> None:
    docs = [
        _doc("cur", title="refund", status=DocStatus.CURRENT),
        _doc("dep", title="refund", status=DocStatus.DEPRECATED),
        _doc("res", title="refund", status=DocStatus.RESOLVED),
    ]
    results = search_docs(docs, "refund", status_filter=DocStatus.CURRENT)
    assert [c.doc_id for c in results] == ["cur"]


def test_status_filter_deprecated_returns_only_deprecated() -> None:
    docs = [
        _doc("cur", title="refund", status=DocStatus.CURRENT),
        _doc("dep", title="refund", status=DocStatus.DEPRECATED),
    ]
    results = search_docs(docs, "refund", status_filter=DocStatus.DEPRECATED)
    assert [c.doc_id for c in results] == ["dep"]


def test_no_status_filter_returns_all_matching_docs() -> None:
    docs = [
        _doc("cur", title="refund", status=DocStatus.CURRENT),
        _doc("dep", title="refund", status=DocStatus.DEPRECATED),
        _doc("res", title="refund", status=DocStatus.RESOLVED),
    ]
    results = search_docs(docs, "refund")
    assert {c.doc_id for c in results} == {"cur", "dep", "res"}


# ---------------------------------------------------------------------------
# ranking_override
# ---------------------------------------------------------------------------


def test_ranking_override_reorders_lower_scoring_doc_to_front() -> None:
    docs = [
        _doc("high_score", title="refund policy cash"),  # 3 title tokens → score 6
        _doc("low_score", title="refund"),               # 1 title token  → score 2
    ]
    # Without override the high-score doc comes first.
    natural = search_docs(docs, "refund policy cash")
    assert natural[0].doc_id == "high_score"

    # Override forces low_score first.
    overridden = search_docs(docs, "refund policy cash", ranking_override=["low_score", "high_score"])
    assert [c.doc_id for c in overridden] == ["low_score", "high_score"]


def test_ranking_override_docs_not_in_list_follow_after() -> None:
    docs = [
        _doc("a", title="refund policy"),
        _doc("b", title="refund policy"),
        _doc("c", title="refund policy"),
    ]
    results = search_docs(docs, "refund policy", ranking_override=["c"])
    assert results[0].doc_id == "c"
    # Remaining two follow by tie-break (doc_id asc).
    assert [c.doc_id for c in results[1:]] == ["a", "b"]


def test_ranking_override_does_not_surface_zero_score_docs() -> None:
    docs = [
        _doc("relevant", title="refund"),
        _doc("irrelevant", title="shipping"),  # no query token overlap → score 0
    ]
    results = search_docs(docs, "refund", ranking_override=["irrelevant", "relevant"])
    # "irrelevant" is in the override but scores 0 — must not appear.
    assert [c.doc_id for c in results] == ["relevant"]


def test_ranking_override_none_is_identical_to_default() -> None:
    docs = [_doc("a", title="refund policy"), _doc("b", title="refund")]
    assert search_docs(docs, "refund policy") == search_docs(
        docs, "refund policy", ranking_override=None
    )


# ---------------------------------------------------------------------------
# Provenance fields on RetrievedChunk
# ---------------------------------------------------------------------------


def test_chunk_carries_doc_id_status_title_content_score_source() -> None:
    doc = Doc(
        doc_id="policy_v4",
        title="Current Refund Policy",
        content="Cash within 30 days.",
        status=DocStatus.CURRENT,
        source="kb://support/policies/refund_policy_v4",
    )
    results = search_docs([doc], "refund")
    assert len(results) == 1
    chunk = results[0]
    assert chunk.doc_id == "policy_v4"
    assert chunk.status == DocStatus.CURRENT
    assert chunk.title == "Current Refund Policy"
    assert chunk.source == "kb://support/policies/refund_policy_v4"
    assert chunk.score > 0


def test_content_is_returned_in_full_without_truncation() -> None:
    long_content = "refund " + ("x " * 5000)
    doc = _doc("big", title="refund", content=long_content)
    results = search_docs([doc], "refund")
    assert results[0].content == long_content


def test_chunk_is_a_retrieved_chunk_instance() -> None:
    docs = [_doc("a", title="refund")]
    results = search_docs(docs, "refund")
    assert all(isinstance(c, RetrievedChunk) for c in results)
