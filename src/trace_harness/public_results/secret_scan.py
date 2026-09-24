"""Refuse to publish a tree that holds a credential.

    python -m trace_harness.public_results.secret_scan [PATH ...]

Greps every file under the given paths for the shapes of the keys this project
handles, and exits 1 on any hit. With no paths it scans ``docs/acceptance/``
and every directory named ``cassettes`` in the repository, which is what the
publish job and a test in the gate run.

It is a grep written in Python for two reasons. The patterns are tested, one
planted sample each. And a hit is printed redacted, because CI logs on a
public repository are public, and a plain ``grep`` would print the very line
that holds the key.

Retained files hold keys in encoded form as often as plain. A cassette or a
trace keeps a model's text as a JSON string, sometimes JSON inside JSON, and a
URL keeps it percent-encoded. Several patterns start on a word boundary, so
that ``task-`` is never read as an OpenAI key, and the character in front of an
encoded key is often a letter of the escape, the ``n`` of ``\\n`` or the ``0`` of
``%20``. Each line is therefore matched twice, as written and after
:func:`unescape` has decoded its JSON and percent escapes, and a key found by
both is reported once.

A match is reported by file, line and pattern name, with the first four
characters of the match and its length. Exit codes: 0 clean, 1 a hit, 2 a path
that does not exist (a typo must not scan nothing and pass).
"""

from __future__ import annotations

import os
import re
import sys
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern))
    for name, pattern in (
        ("google api key", r"AIza[0-9A-Za-z_\-]{35}"),
        # The Gemini keys this project has used start with "AQ." (#179).
        ("google AQ key", r"\bAQ\.[0-9A-Za-z_\-]{20,}"),
        ("anthropic key", r"\bsk-ant-[0-9A-Za-z_\-]{20,}"),
        ("openai key", r"\bsk-(?:proj-|svcacct-|admin-)?[0-9A-Za-z_\-]{20,}"),
        ("supabase secret key", r"\bsb_secret_[0-9A-Za-z_\-]{16,}"),
        ("supabase access token", r"\bsbp_[0-9a-f]{40}\b"),
        # Supabase's legacy anon and service_role keys, and any other JWT.
        (
            "jwt",
            r"\beyJ[0-9A-Za-z_\-]{10,}\.eyJ[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}",
        ),
        ("github token", r"\b(?:gh[pousr]_[0-9A-Za-z]{36,}|github_pat_[0-9A-Za-z_]{22,})"),
        ("aws access key id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        (
            "authorization header",
            r"(?i)\bauthorization\"?\s*[:=]\s*\"?(?:bearer|basic|token)\s+[0-9A-Za-z._~+/\-]{8,}",
        ),
        (
            "api key header",
            r"(?i)\b(?:x-api-key|x-goog-api-key|apikey|api[_-]key)\"?\s*[:=]\s*\"?"
            r"[0-9A-Za-z._\-]{20,}",
        ),
    )
)

DEFAULT_ROOT = "docs/acceptance"
CASSETTE_DIR_NAME = "cassettes"
_SKIP_DIRS = {".git", "node_modules", ".next", ".venv", "venv", "__pycache__"}


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    pattern: str
    preview: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.pattern} ({self.preview})"


def _redact(match: str) -> str:
    return f"{match[:4]}... {len(match)} chars"


def default_targets(repo_root: Path) -> list[Path]:
    """``docs/acceptance/`` and every ``cassettes`` directory in the repository."""
    targets = [repo_root / DEFAULT_ROOT]
    for directory, children, _files in os.walk(repo_root):
        children[:] = sorted(c for c in children if c not in _SKIP_DIRS)
        here = Path(directory)
        if here.name == CASSETTE_DIR_NAME:
            targets.append(here)
            children[:] = []
    return targets


def _files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    found = []
    for directory, children, files in os.walk(target):
        children[:] = sorted(c for c in children if c not in _SKIP_DIRS)
        found.extend(Path(directory) / name for name in sorted(files))
    return found


_JSON_ESCAPE = re.compile(r"\\(?:u([0-9A-Fa-f]{4})|([\"\\/bfnrt]))")
_JSON_SIMPLE = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
# Each level of JSON inside JSON doubles the backslashes, so a handful of passes
# covers any nesting a retained file has. The cap bounds the work on a line of
# nothing but backslashes.
_MAX_UNESCAPE_PASSES = 8


def _json_escape(match: re.Match[str]) -> str:
    code, simple = match.groups()
    return chr(int(code, 16)) if code is not None else _JSON_SIMPLE[simple]


def unescape(text: str) -> str:
    """``text`` with its JSON string escapes and percent escapes decoded.

    One pass decodes ``\\n``, ``\\t``, ``\\"``, ``\\uXXXX`` and the other JSON
    escapes, then ``%XX`` sequences as UTF-8. Passes repeat until nothing
    changes, so JSON inside JSON and a JSON escape inside a URL come out plain.
    A backslash or percent sign that starts no escape is left as it is. Pure,
    so any caller that holds text can use it.
    """
    for _ in range(_MAX_UNESCAPE_PASSES):
        decoded = _JSON_ESCAPE.sub(_json_escape, text)
        if _PERCENT_ESCAPE.search(decoded):
            decoded = urllib.parse.unquote(decoded, errors="replace")
        if decoded == text:
            break
        text = decoded
    return text


def scan_text(text: str, path: str) -> list[Hit]:
    """Every key pattern match in ``text``, reported against ``path``.

    Each line is matched as written and after :func:`unescape`. A match the
    decoded line adds beyond those of the line as written is a key hidden
    behind an escape, and each occurrence is reported once. Pure: it reads and
    writes nothing.
    """
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        decoded = unescape(line)
        for name, pattern in PATTERNS:
            as_written = [match.group(0) for match in pattern.finditer(line)]
            found = list(as_written)
            if decoded != line:
                behind_escapes = Counter(m.group(0) for m in pattern.finditer(decoded))
                found += list((behind_escapes - Counter(as_written)).elements())
            hits.extend(Hit(path, number, name, _redact(match)) for match in found)
    return hits


def scan(targets: list[Path]) -> tuple[int, list[Hit]]:
    """Scan every file under ``targets``. Returns (files scanned, hits)."""
    missing = [str(t) for t in targets if not t.exists()]
    if missing:
        raise FileNotFoundError(f"scan target not found: {', '.join(missing)}")
    scanned, hits = 0, []
    for target in targets:
        for path in _files(target):
            scanned += 1
            text = path.read_bytes().decode("utf-8", errors="replace")
            hits.extend(scan_text(text, str(path)))
    return scanned, hits


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    targets = [Path(a) for a in args] if args else default_targets(Path.cwd())
    try:
        scanned, hits = scan(targets)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    where = ", ".join(str(t) for t in targets)
    if hits:
        for hit in hits:
            print(hit, file=sys.stderr)
        print(f"{len(hits)} key pattern hit(s) in {scanned} file(s) under {where}", file=sys.stderr)
        return 1
    print(f"no key patterns in {scanned} file(s) under {where}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
