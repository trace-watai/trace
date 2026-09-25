r"""Find credentials in text and files before evidence is retained or published.

One scanner for every place that commits or uploads evidence. Sweep retention
(#198) runs it over a folder of failing cells before the folder lands under
``docs/acceptance``, and ``python -m trace_harness.public_results.secret_scan``
(#205) runs it over ``docs/acceptance`` and every ``cassettes`` folder before
the publish job uploads them. Anything else that publishes evidence can import
the same two functions.

``scan_text(text, path, values=...)``
    Pure. Returns a :class:`SecretHit` for every line of ``text`` that holds
    one of :data:`SHAPES` or one of the literal ``values``.
``scan_paths(targets, values=..., relative_to=...)``
    Reads every file under ``targets`` and scans it. A target that does not
    exist raises :class:`FileNotFoundError`, so a mistyped path cannot scan
    nothing and pass.

A hit names the file, the line and the kind of secret. It never carries the
matched text, so printing a hit cannot print the key.

Escaped text
    A key inside a JSON string that was itself written as a string, such as a
    request body inside a cassette line or a tool argument inside a trace,
    sits behind escapes like ``\n`` and ``\"``. A key in a URL sits behind
    ``%XX``. Several shapes begin at a word boundary so that ``task-`` never
    reads as ``sk-``, and the last character of an escape (the ``n`` of
    ``\n``, the ``0`` of ``%20``) is a word character, which removes that
    boundary. So every line is matched as written and again after decoding
    ``\n \r \t \b \f \" \/ \\``, ``\uXXXX`` and ``%XX``. Decoding repeats
    until it changes nothing, up to :data:`MAX_DECODE_PASSES` times, which
    also reaches a key escaped twice. The line as written is always one of
    the readings, so decoding can add a hit and never lose one.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: The provider key variables this project reads.
PROVIDER_KEY_VARIABLES = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")

#: Every key shape the scanner knows, as (kind, pattern).
SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(pattern))
    for kind, pattern in (
        ("Google API key", r"AIza[0-9A-Za-z_\-]{35}"),
        # The Gemini keys this project has used start with "AQ." (#179).
        ("Google AQ. key", r"\bAQ\.[0-9A-Za-z_\-]{20,}"),
        ("Anthropic key", r"\bsk-ant-[0-9A-Za-z_\-]{20,}"),
        # OpenAI's sk-, sk-proj-, sk-svcacct- and sk-admin- keys.
        ("OpenAI key", r"\bsk-(?!ant-)[0-9A-Za-z_\-]{20,}"),
        ("Supabase secret key", r"\bsb_secret_[0-9A-Za-z_\-]{16,}"),
        ("Supabase access token", r"\bsbp_[0-9a-f]{40}\b"),
        # Supabase's legacy anon and service_role keys, and any other JWT.
        ("JWT", r"\beyJ[0-9A-Za-z_\-]{10,}\.eyJ[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}"),
        ("GitHub token", r"\b(?:gh[pousr]_[0-9A-Za-z]{36,}|github_pat_[0-9A-Za-z_]{22,})"),
        ("AWS access key id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        ("bearer token", r"(?i)\bbearer\s+[0-9A-Za-z._~+/\-]{16,}"),
        (
            "authorization header",
            r"(?i)\bauthorization\"?\s*[:=]\s*\"?(?:bearer|basic|token)\s+[0-9A-Za-z._~+/\-]{8,}",
        ),
        (
            "API key header",
            r"(?i)\b(?:x-api-key|x-goog-api-key|apikey|api[_-]key)\"?\s*[:=]\s*\"?"
            r"[0-9A-Za-z._\-]{20,}",
        ),
        # A JSON field named like an auth header that holds a string, whatever
        # the string is. A recording has no reason to keep one, and a key of
        # a shape listed nowhere above would still be caught here. A null or
        # empty value holds nothing and passes.
        (
            "auth header field",
            r'(?i)"(?:authorization|proxy[-_]authorization|x[-_]api[-_]key|'
            r'x[-_]goog[-_]api[-_]key|api[-_]?key)"\s*:\s*"[^"]',
        ),
    )
)

#: How many times a line is decoded at most. Each pass removes one layer of
#: escaping, and no evidence file escapes a string more than twice.
MAX_DECODE_PASSES = 8
#: Literal values shorter than this are ignored, since they would match text
#: that is not a secret.
MIN_VALUE_LENGTH = 8

_SKIP_DIRS = frozenset({".git", "node_modules", ".next", ".venv", "venv", "__pycache__"})
_JSON_ESCAPE = re.compile(r'\\(?:(["\\/bfnrt])|u([0-9A-Fa-f]{4}))')
_JSON_CHARS = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")


@dataclass(frozen=True)
class SecretHit:
    """One kind of secret on one line of one file. Never the secret itself."""

    path: str
    line: int
    kind: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}"


def readings(line: str) -> list[str]:
    """``line`` as written, then decoded once, twice, and so on until it settles."""
    forms = [line]
    while len(forms) <= MAX_DECODE_PASSES and ("\\" in forms[-1] or "%" in forms[-1]):
        decoded = _decode_once(forms[-1])
        if decoded == forms[-1]:
            break
        forms.append(decoded)
    return forms


def _decode_once(text: str) -> str:
    text = _JSON_ESCAPE.sub(
        lambda m: _JSON_CHARS[m[1]] if m[1] else chr(int(m[2], 16)),
        text,
    )
    return _PERCENT.sub(lambda m: chr(int(m[1], 16)), text)


def scan_text(
    text: str, path: str = "<text>", values: Iterable[tuple[str, str]] = ()
) -> list[SecretHit]:
    """Every (line, kind) in ``text`` that holds a key shape or a literal value.

    ``values`` are (kind, literal) pairs, such as the provider keys set in the
    environment, each searched for as a plain substring of every reading of a
    line. Literals shorter than :data:`MIN_VALUE_LENGTH` are ignored.
    """
    literals = [(kind, value) for kind, value in values if len(value) >= MIN_VALUE_LENGTH]
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        forms = readings(line)
        kinds = [kind for kind, value in literals if any(value in form for form in forms)]
        kinds += [kind for kind, pattern in SHAPES if any(pattern.search(f) for f in forms)]
        hits += [SecretHit(path, number, kind) for kind in dict.fromkeys(kinds)]
    return hits


def files_under(targets: Sequence[Path]) -> list[Path]:
    """Every file under ``targets``, in a stable order, skipping tool folders."""
    missing = [str(target) for target in targets if not target.exists()]
    if missing:
        raise FileNotFoundError(f"scan target not found: {', '.join(missing)}")
    found = []
    for target in targets:
        if target.is_file():
            found.append(target)
            continue
        for directory, children, files in os.walk(target):
            children[:] = sorted(c for c in children if c not in _SKIP_DIRS)
            found += [Path(directory) / name for name in sorted(files)]
    return found


def scan_paths(
    targets: Sequence[Path],
    values: Iterable[tuple[str, str]] = (),
    relative_to: Path | None = None,
) -> list[SecretHit]:
    """Scan every file under ``targets``.

    A hit's path is relative to ``relative_to`` when given, and as found
    otherwise. Files are read as UTF-8 with undecodable bytes replaced, so a
    binary file is scanned for whatever text it holds.
    """
    literals = list(values)
    hits = []
    for path in files_under(targets):
        shown = path.relative_to(relative_to).as_posix() if relative_to else str(path)
        text = path.read_bytes().decode("utf-8", errors="replace")
        hits += scan_text(text, shown, literals)
    return hits


def key_values(names: Sequence[str] = PROVIDER_KEY_VARIABLES) -> list[tuple[str, str]]:
    """The provider keys set in the environment, as ``values`` for a scan."""
    return [(f"value of {name}", os.environ.get(name, "")) for name in names]
