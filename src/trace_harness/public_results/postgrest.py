"""A small PostgREST client over the standard library.

Supabase serves every table in ``public`` at ``{url}/rest/v1/{table}`` through
PostgREST. Reading a table is a GET with ``select``, filter, ``order``,
``limit`` and ``offset`` parameters, and PostgREST reports the total in a
``Content-Range`` header when asked with ``Prefer: count=exact``. An upsert is a
POST with ``on_conflict`` and ``Prefer: resolution=merge-duplicates``. That is
all the harness needs, so it talks HTTP with ``urllib`` and adds no dependency.

The transport is a plain callable from :class:`HttpRequest` to
:class:`HttpResponse`. Tests pass one that answers from synthesized responses
and never open a socket. :func:`urllib_transport` is the only code here that
reaches the network.

Keys. Supabase's publishable and secret keys (``sb_publishable_...`` and
``sb_secret_...``) go in the ``apikey`` header only, because they are not JWTs.
The legacy ``anon`` and ``service_role`` keys are JWTs and also go in
``Authorization: Bearer``, which is how PostgREST picks the role for them. A
key never appears in an error message.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

DEFAULT_PAGE_SIZE = 1000  # Supabase's default max-rows per response.
DEFAULT_TIMEOUT_S = 30.0
USER_AGENT = "trace-harness-public-results"


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None = None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    # Header names are lower-cased by whoever builds the response.
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


Transport = Callable[[HttpRequest], HttpResponse]


class PostgrestError(RuntimeError):
    """A request PostgREST refused, or one that never got an answer.

    ``code`` is PostgREST's or Postgres's error code when the body carried one,
    for example ``42501`` for a missing privilege or ``PGRST205`` for a table
    that does not exist.
    """

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def urllib_transport(request: HttpRequest, *, timeout: float = DEFAULT_TIMEOUT_S) -> HttpResponse:
    """Send one request with urllib. HTTP error statuses come back as responses."""
    raw = urllib.request.Request(
        request.url, data=request.body, method=request.method, headers=dict(request.headers)
    )
    try:
        with urllib.request.urlopen(raw, timeout=timeout) as response:  # noqa: S310 (https only)
            headers = {k.lower(): v for k, v in response.headers.items()}
            return HttpResponse(response.status, headers, response.read())
    except urllib.error.HTTPError as exc:
        headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return HttpResponse(exc.code, headers, exc.read() or b"")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        host = urllib.parse.urlsplit(request.url).netloc
        raise PostgrestError(f"could not reach {host}: {exc}") from None


def jwt_role(key: str) -> str | None:
    """The ``role`` claim of a legacy JWT key, read without verifying it.

    Used only to refuse an obviously wrong key early. Returns None for the new
    key formats and for anything that does not parse.
    """
    parts = key.split(".")
    if len(parts) != 3 or not key.startswith("eyJ"):
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except ValueError:
        return None
    role = claims.get("role") if isinstance(claims, dict) else None
    return role if isinstance(role, str) else None


def key_role(key: str) -> str | None:
    """Which Postgres role a Supabase key maps to, when that can be told offline."""
    if key.startswith("sb_publishable_"):
        return "anon"
    if key.startswith("sb_secret_"):
        return "service_role"
    return jwt_role(key)


def normalize_base_url(url: str) -> str:
    """The project URL without a trailing slash. Plain http is allowed only locally."""
    parsed = urllib.parse.urlsplit(url.strip())
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError(f"Supabase URL must start with https:// (got {url!r})")
    if not parsed.netloc:
        raise ValueError(f"Supabase URL has no host (got {url!r})")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _content_range_total(value: str | None) -> int | None:
    """The total from ``Content-Range: 0-24/3573458`` or ``*/0``; None for ``/*``."""
    if not value or "/" not in value:
        return None
    total = value.rsplit("/", 1)[1].strip()
    return int(total) if total.isdigit() else None


def _in_list(values: Iterable[str]) -> str:
    quoted = []
    for value in values:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        quoted.append(f'"{escaped}"')
    return f"in.({','.join(quoted)})"


class PostgrestClient:
    """Reads and writes tables in one Supabase project's ``public`` schema."""

    def __init__(
        self,
        base_url: str,
        key: str,
        *,
        transport: Transport = urllib_transport,
        page_size: int = DEFAULT_PAGE_SIZE,
    ):
        if not key:
            raise ValueError("a Supabase API key is required")
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        self.base_url = normalize_base_url(base_url)
        self._key = key
        self.transport = transport
        self.page_size = page_size

    def __repr__(self) -> str:  # never show the key
        return f"PostgrestClient({self.base_url!r})"

    @property
    def role(self) -> str | None:
        """The Postgres role the key maps to, when that can be told offline."""
        return key_role(self._key)

    # --- reads ---

    def select(
        self,
        table: str,
        columns: str,
        *,
        filters: Mapping[str, str] | None = None,
        order: str | None = None,
    ) -> list[dict[str, Any]]:
        """Every matching row, fetched page by page until Content-Range's total."""
        rows: list[dict[str, Any]] = []
        while True:
            params = [("select", columns), *(filters or {}).items()]
            if order:
                params.append(("order", order))
            params += [("limit", str(self.page_size)), ("offset", str(len(rows)))]
            response = self._send("GET", table, params, prefer="count=exact")
            page = self._json_rows(response, table)
            rows.extend(page)
            total = _content_range_total(response.headers.get("content-range"))
            if not page:
                break
            if total is None:
                if len(page) < self.page_size:
                    break
            elif len(rows) >= total:
                break
        return rows

    def select_one(
        self, table: str, columns: str, key_column: str, key: str
    ) -> dict[str, Any] | None:
        """The row whose ``key_column`` equals ``key``, or None."""
        params = [("select", columns), (key_column, f"eq.{key}")]
        rows = self._json_rows(self._send("GET", table, params), table)
        if len(rows) > 1:
            raise PostgrestError(f"{table}: {len(rows)} rows for {key_column}={key!r}")
        return rows[0] if rows else None

    # --- writes ---

    def upsert(self, table: str, rows: list[dict[str, Any]], *, on_conflict: str) -> None:
        """Insert ``rows``, replacing any row with the same ``on_conflict`` key."""
        if not rows:
            return
        body = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = self._send(
            "POST",
            table,
            [("on_conflict", on_conflict)],
            prefer="resolution=merge-duplicates,return=minimal",
            body=body,
        )
        self._check(response, table)

    def delete(self, table: str, key_column: str, keys: list[str]) -> None:
        """Delete the rows whose ``key_column`` is one of ``keys``."""
        if not keys:
            return
        response = self._send(
            "DELETE", table, [(key_column, _in_list(keys))], prefer="return=minimal"
        )
        self._check(response, table)

    # --- internals ---

    def _headers(self, prefer: str | None, has_body: bool) -> dict[str, str]:
        headers = {"apikey": self._key, "Accept": "application/json", "User-Agent": USER_AGENT}
        if jwt_role(self._key) is not None:
            headers["Authorization"] = f"Bearer {self._key}"
        if prefer:
            headers["Prefer"] = prefer
        if has_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _send(
        self,
        method: str,
        table: str,
        params: list[tuple[str, str]],
        *,
        prefer: str | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        query = urllib.parse.urlencode(params, safe=",.()*:\"'", quote_via=urllib.parse.quote)
        url = f"{self.base_url}/rest/v1/{table}?{query}"
        request = HttpRequest(method, url, self._headers(prefer, body is not None), body)
        return self.transport(request)

    def _check(self, response: HttpResponse, table: str) -> None:
        if 200 <= response.status < 300:
            return
        code, message = None, response.body.decode("utf-8", "replace").strip()
        try:
            data = json.loads(response.body)
        except ValueError:
            data = None
        if isinstance(data, dict):
            code = data.get("code") if isinstance(data.get("code"), str) else None
            message = str(data.get("message") or message)
            if data.get("hint"):
                message += f" ({data['hint']})"
        raise PostgrestError(
            f"{table}: HTTP {response.status}: {message or 'no body'}",
            status=response.status,
            code=code,
        )

    def _json_rows(self, response: HttpResponse, table: str) -> list[dict[str, Any]]:
        self._check(response, table)
        try:
            data = json.loads(response.body)
        except ValueError as exc:
            raise PostgrestError(f"{table}: response is not JSON: {exc}") from None
        if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
            raise PostgrestError(f"{table}: expected a JSON array of rows")
        return data
