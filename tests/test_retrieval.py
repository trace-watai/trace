"""Unit tests for deterministic keyword retrieval."""

from __future__ import annotations

from trace_harness.environment.retrieval import search_docs
from trace_harness.environment.state import Doc, DocStatus


def _doc(
    doc_id: str,
    title: str,
    content: str,
    status: DocStatus = DocStatus.CURRENT,
) -> Doc:
    return Doc(doc_id=doc_id, title=title, status=status, content=content)


# --- scoring ---


def test_title_hit_scores_2_content_hit_scores_1():
    docs = [
        _doc("title-only", title="refund policy", content="something else"),
        _doc("content-only", title="other title", content="refund here"),
        _doc("both", title="refund policy", content="refund here"),
    ]
    results = search_docs(docs, "refund")
    by_id = {r.doc_id: r for r in results}
    assert by_id["title-only"].score == 2.0
    assert by_id["content-only"].score == 1.0
    assert by_id["both"].score == 3.0


def test_multi_token_query_accumulates_scores():
    # "cash" and "refund" both appear in title → 2.0 + 2.0 = 4.0
    doc = _doc("doc", title="cash refund", content="other")
    results = search_docs([doc], "cash refund")
    assert len(results) == 1
    assert results[0].score == 4.0


def test_zero_score_docs_excluded():
    docs = [
        _doc("match", title="refund", content="policy"),
        _doc("no-match", title="unrelated", content="totally different"),
    ]
    results = search_docs(docs, "refund policy")
    ids = {r.doc_id for r in results}
    assert "match" in ids
    assert "no-match" not in ids


# --- ordering ---


def test_ties_break_by_doc_id_ascending():
    docs = [
        _doc("zzz-doc", title="refund", content="x"),
        _doc("aaa-doc", title="refund", content="x"),
    ]
    results = search_docs(docs, "refund")
    assert len(results) == 2
    assert results[0].doc_id == "aaa-doc"
    assert results[1].doc_id == "zzz-doc"


# --- filtering ---


def test_status_filter_excludes_non_matching():
    docs = [
        _doc("current-doc", title="refund policy", content="x", status=DocStatus.CURRENT),
        _doc("deprecated-doc", title="refund policy", content="x", status=DocStatus.DEPRECATED),
    ]
    results = search_docs(docs, "refund", status_filter=DocStatus.CURRENT)
    ids = {r.doc_id for r in results}
    assert "current-doc" in ids
    assert "deprecated-doc" not in ids


def test_top_k_limits_results():
    docs = [_doc(f"doc-{i}", title="refund", content="x") for i in range(10)]
    results = search_docs(docs, "refund", top_k=3)
    assert len(results) == 3


# --- content fidelity ---


def test_content_not_truncated():
    long_content = "refund " + ("policy text " * 200)
    doc = _doc("long-doc", title="policy", content=long_content)
    results = search_docs([doc], "refund policy")
    assert len(results) == 1
    assert results[0].content == long_content


def test_chunk_carries_status_metadata():
    docs = [
        _doc("cur", title="refund", content="x", status=DocStatus.CURRENT),
        _doc("dep", title="refund", content="x", status=DocStatus.DEPRECATED),
        _doc("res", title="refund", content="x", status=DocStatus.RESOLVED),
    ]
    results = search_docs(docs, "refund")
    by_id = {r.doc_id: r for r in results}
    assert by_id["cur"].status == DocStatus.CURRENT
    assert by_id["dep"].status == DocStatus.DEPRECATED
    assert by_id["res"].status == DocStatus.RESOLVED


# --- edge cases ---


def test_empty_docs_returns_empty():
    assert search_docs([], "refund policy") == []


def test_empty_query_returns_empty():
    doc = _doc("doc", title="refund", content="policy")
    # A query with no alphanumeric tokens produces no query_tokens, so no matches.
    results = search_docs([doc], "!!! ---")
    assert results == []


def test_determinism():
    docs = [
        _doc("b-doc", title="refund policy", content="cash refund"),
        _doc("a-doc", title="cash", content="refund policy"),
    ]
    first = search_docs(docs, "refund policy cash")
    second = search_docs(docs, "refund policy cash")
    assert [r.doc_id for r in first] == [r.doc_id for r in second]
    assert [r.score for r in first] == [r.score for r in second]
