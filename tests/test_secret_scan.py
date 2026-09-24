"""The shared secret scanner (#198): every key shape, as written and behind escapes.

Each shape has a planted sample, and each sample is planted again behind every
escape a retained or published file can put in front of it. The escapes are the
reason the scanner decodes before matching. A shape that starts at a word
boundary misses a key right after ``\\n`` or ``%20``, because the escape's last
character is a word character.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest

from trace_harness.secret_scan import (
    MAX_DECODE_PASSES,
    SHAPES,
    SecretHit,
    files_under,
    readings,
    scan_paths,
    scan_text,
)

# Built at run time so no key-shaped literal sits in the repository.
PLANTED = {
    "Google API key": "AIza" + "B" * 35,
    "Google AQ. key": "AQ." + "Ab8RN6" * 5,
    "Anthropic key": "sk-ant-" + "api03-" + "x" * 30,
    "OpenAI key": "sk-" + "proj-" + "Y" * 40,
    "Supabase secret key": "sb_" + "secret_" + "Z" * 32,
    "Supabase access token": "sbp_" + "a1" * 20,
    "JWT": ".".join(["eyJ" + "h" * 20, "eyJ" + "p" * 30, "s" * 43]),
    "GitHub token": "ghp_" + "G" * 36,
    "AWS access key id": "AKIA" + "Q" * 16,
    "private key": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "bearer token": "Bearer " + "t" * 24,
    "authorization header": "Authorization: Basic " + "u" * 12,
    "API key header": "x-api-key: " + "k" * 39,
    "auth header field": '"x-goog-api-key": "' + "redacted" + '"',
}


def _json_string(text: str) -> str:
    """``text`` as a JSON string literal, quotes and all."""
    return json.dumps(text)


def _after(char: str):
    """A JSON string where the sample follows ``char``, which JSON escapes."""
    return lambda sample: _json_string("line" + char + sample)


# Each context puts a sample where a retained or published file could hold it.
CONTEXTS = {
    "as written": lambda s: '{"note": "x ' + s + ' y"}',
    **{
        f"after \\{name}": _after(char)
        for name, char in (("n", "\n"), ("r", "\r"), ("t", "\t"), ("b", "\b"), ("f", "\f"))
    },
    'inside \\"': lambda s: _json_string('he said "' + s + '"'),
    "after \\/": lambda s: '"https:\\/\\/example.com\\/' + json.dumps(s)[1:-1] + '"',
    "after \\\\": lambda s: _json_string("C:\\" + s),
    "after \\u2028": lambda s: json.dumps("line\u2028" + s),
    "escaped twice": lambda s: _json_string(_json_string("line\n" + s)),
    "after %20": lambda s: "https://example.com/?q=hello%20" + quote(s, safe=""),
    "after %0A": lambda s: "body=line%0A" + quote(s, safe=""),
    "after %2520": lambda s: (
        "https://example.com/?q=hello%2520" + quote(quote(s, safe=""), safe="")
    ),
}


def test_every_shape_has_a_planted_sample() -> None:
    assert set(PLANTED) == {kind for kind, _ in SHAPES}


@pytest.mark.parametrize("context", sorted(CONTEXTS))
@pytest.mark.parametrize("kind", sorted(PLANTED))
def test_every_shape_is_found_behind_every_escape(kind: str, context: str) -> None:
    line = CONTEXTS[context](PLANTED[kind])
    assert kind in {hit.kind for hit in scan_text(line)}, line


def test_escapes_are_what_the_word_boundary_misses() -> None:
    """The escaped samples above are ones a match on the text as written misses."""
    missed = {
        (kind, context)
        for kind, pattern in SHAPES
        for context, build in CONTEXTS.items()
        if not pattern.search(build(PLANTED[kind]))
    }
    assert ("Google AQ. key", "after \\n") in missed
    assert ("Anthropic key", "after %20") in missed
    assert ("OpenAI key", "escaped twice") in missed
    assert ("bearer token", "after \\t") in missed
    assert ("auth header field", 'inside \\"') in missed


def test_a_hit_never_carries_the_secret() -> None:
    for sample in PLANTED.values():
        for hit in scan_text(CONTEXTS["after \\n"](sample), "trace.jsonl"):
            assert sample not in str(hit)
            assert sample not in repr(hit)
            assert str(hit) == f"trace.jsonl:1: {hit.kind}"


def test_values_are_found_as_written_and_escaped() -> None:
    value = "not-a-shape+value/0123456789"
    values = [("value of SOME_KEY", value)]
    assert [h.kind for h in scan_text("x" + value, values=values)] == ["value of SOME_KEY"]
    escaped = quote(value, safe="")
    assert value not in escaped
    assert [h.kind for h in scan_text("q=" + escaped, values=values)] == ["value of SOME_KEY"]
    assert scan_text(json.dumps(value).replace("/", "\\/"), values=values) != []
    # A short or empty value would match text that is no secret.
    assert scan_text("abc", values=[("value of EMPTY", ""), ("value of SHORT", "abc")]) == []


def test_clean_text_has_no_hits() -> None:
    """Near misses, and the null and empty fields a recording may keep."""
    lines = [
        '{"task": "sk-", "AQ.": 1, "api_key": null, "authorization": ""}',
        '{"task_id": "refund_task-assessment-for-the-customer-account"}',
        '{"note": "ask-the-manager-before-issuing-any-refund"}',
        "the FAQ.html page explains the refund-policy-for-annual-plans",
        "Bearer short",
    ]
    assert [hit for line in lines for hit in scan_text(line)] == []


def test_each_kind_is_reported_once_per_line() -> None:
    sample = PLANTED["Google API key"]
    text = f"{sample} {sample}\nclean\n{_json_string(chr(10) + sample)}\n"
    assert scan_text(text, "f") == [
        SecretHit("f", 1, "Google API key"),
        SecretHit("f", 3, "Google API key"),
    ]


def test_decoding_stops_once_the_line_settles() -> None:
    assert readings("plain") == ["plain"]
    assert readings("a%2520b") == ["a%2520b", "a%20b", "a b"]
    # "A" percent-encoded 21 times over.
    deep = "%" + "25" * 20 + "41"
    assert len(readings(deep)) == MAX_DECODE_PASSES + 1


def test_scan_paths_walks_targets_and_refuses_a_missing_one(tmp_path: Path) -> None:
    (tmp_path / "runs" / "run_1").mkdir(parents=True)
    (tmp_path / "runs" / "run_1" / "trace.jsonl").write_text(
        '{"ok": 1}\n' + CONTEXTS["after \\n"](PLANTED["OpenAI key"]) + "\n"
    )
    (tmp_path / "runs" / "clean.json").write_text('{"api_key": null}\n')
    (tmp_path / "runs" / "node_modules").mkdir()
    (tmp_path / "runs" / "node_modules" / "x.txt").write_text(PLANTED["OpenAI key"])

    root = tmp_path / "runs"
    assert [p.name for p in files_under([root])] == ["clean.json", "trace.jsonl"]
    assert scan_paths([root], relative_to=root) == [SecretHit("run_1/trace.jsonl", 2, "OpenAI key")]
    assert scan_paths([root / "clean.json"]) == []
    with pytest.raises(FileNotFoundError, match="not found"):
        scan_paths([root / "typo"])
