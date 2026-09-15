"""Explicit, single-run record/replay at the normalized model-adapter boundary.

Requests are fingerprints, never raw prompts or SDK request objects. Responses
contain action fields and allowlisted token counts, never raw SDK responses.
Replay loads and validates the entire cassette before serving any actions.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from trace_harness.models.base import (
    AgentAction,
    Message,
    ModelAdapter,
    ModelAdapterError,
    ToolSpec,
)

CASSETTE_SCHEMA_VERSION = "0.1"
TOKEN_FIELDS = frozenset(
    {
        "prompt_token_count",
        "candidates_token_count",
        "total_token_count",
        "cached_content_token_count",
        "thoughts_token_count",
        "tool_use_prompt_token_count",
    }
)
_SECRET_FIELDS = frozenset(
    {"apikey", "authorization", "proxyauthorization", "headers", "httpheaders", "accesstoken"}
)


class CassetteError(ModelAdapterError, ValueError):
    """Invalid cassette, unsafe response, or request miss; never a live fallback."""


class CassetteConfig(BaseModel):
    """Shared CLI/suite/run configuration; absent means ordinary adapter use."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["record", "replay"]
    directory: str = Field(default="fixtures/cassettes", min_length=1)


class CassetteRequestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    temperature: float | None = None
    seed: int | None = None
    timeout_seconds: float = Field(default=120.0, gt=0)
    prompt_version: str = "v0"


class CassetteEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["0.1"] = CASSETTE_SCHEMA_VERSION
    cassette_id: str
    config: CassetteRequestConfig
    step: int = Field(ge=1)
    transcript_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tools_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    response: dict[str, Any]
    usage: dict[str, int] = Field(default_factory=dict)


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def cassette_path(directory: Path | str, config: CassetteRequestConfig) -> Path:
    def component(value: str) -> str:
        if value in {".", ".."}:
            raise CassetteError("invalid cassette path component")
        return quote(value, safe="")

    seed = "default" if config.seed is None else str(config.seed)
    return Path(directory) / component(config.task_id) / component(config.model) / f"{seed}.jsonl"


def _check_response(value: Any, secret: str | None = None) -> None:
    """Reject credential-bearing action fields without echoing their contents."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("_", "").replace("-", "")
            if normalized in _SECRET_FIELDS:
                raise CassetteError("credential or header field in cassette response")
            _check_response(child, secret)
    elif isinstance(value, list):
        for child in value:
            _check_response(child, secret)
    elif isinstance(value, str) and secret and secret in value:
        raise CassetteError("credential value in cassette response")


def safe_response(action: AgentAction, *, secret: str | None = None) -> tuple[dict, dict]:
    response = action.model_dump(mode="json", exclude={"raw"})
    _check_response(response, secret)
    raw_usage = (action.raw or {}).get("usage_metadata", {})
    usage = (
        {k: v for k, v in raw_usage.items() if k in TOKEN_FIELDS and type(v) is int and v >= 0}
        if isinstance(raw_usage, dict)
        else {}
    )
    return response, usage


def _action(entry: CassetteEntry) -> AgentAction:
    # AgentAction normally ignores unknown fields. At a disk boundary that would
    # hide a raw SDK payload or a misspelled field, so validate the exact shape.
    allowed = set(AgentAction.model_fields) - {"raw"}
    if set(entry.response) != allowed:
        raise CassetteError("invalid cassette response fields")
    _check_response(entry.response)
    if any(k not in TOKEN_FIELDS or type(v) is not int or v < 0 for k, v in entry.usage.items()):
        raise CassetteError("invalid cassette usage fields")
    try:
        action = AgentAction.model_validate(entry.response)
        if action.model_dump(mode="json", exclude={"raw"}) != entry.response:
            raise ValueError("response does not round trip")
    except ValueError:
        raise CassetteError("invalid cassette action") from None
    return action.model_copy(
        update={"raw": {"usage_metadata": entry.usage} if entry.usage else None}
    )


class RecordingModelAdapter:
    """Wrap any adapter to record; replay needs no inner adapter, SDK, or key.

    A cassette is an ordered sequence: both the transcript (including provider
    state) and tool declarations must match at the current step. Record refuses
    to overwrite an existing file. Instances, like fixture adapters, serve one run.
    """

    def __init__(
        self,
        *,
        mode: Literal["record", "replay"],
        path: Path | str,
        config: CassetteRequestConfig,
        inner: ModelAdapter | None = None,
    ) -> None:
        if mode not in {"record", "replay"}:
            raise CassetteError("cassette mode must be record or replay")
        if mode == "record" and inner is None:
            raise CassetteError("record mode requires an inner model adapter")
        if mode == "replay" and inner is not None:
            raise CassetteError("replay mode must not have an inner model adapter")
        self.name = config.provider
        self.mode = mode
        self.path = Path(path)
        self.config = config
        self._inner = inner
        self._cursor = 0
        self._cassette_id = fingerprint(config.model_dump(mode="json"))
        self._entries: list[CassetteEntry] = []
        if mode == "replay":
            self._entries = self._load()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.path.open("x", encoding="utf-8").close()
            except FileExistsError:
                raise CassetteError("cassette already exists; choose a new directory") from None

    def _load(self) -> list[CassetteEntry]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            raise CassetteError("replay cassette not found") from None
        if not lines:
            raise CassetteError("replay cassette is empty")
        entries: list[CassetteEntry] = []
        for step, line in enumerate(lines, 1):
            try:
                data = json.loads(line)
                if (
                    not isinstance(data, dict)
                    or data.get("schema_version") != CASSETTE_SCHEMA_VERSION
                ):
                    raise CassetteError("unsupported cassette schema version")
                entry = CassetteEntry.model_validate(data)
            except CassetteError:
                raise
            except ValueError:
                raise CassetteError(f"invalid cassette entry at line {step}") from None
            if entry.step != step:
                raise CassetteError(f"cassette step sequence mismatch at line {step}")
            if entry.config != self.config or entry.cassette_id != self._cassette_id:
                raise CassetteError("cassette model configuration mismatch")
            _action(entry)
            entries.append(entry)
        return entries

    def next_action(self, transcript: list[Message], tools: list[ToolSpec]) -> AgentAction:
        step = self._cursor + 1
        transcript_hash = fingerprint([m.model_dump(mode="json") for m in transcript])
        tools_hash = fingerprint([t.model_dump(mode="json") for t in tools])
        if self.mode == "replay":
            if self._cursor >= len(self._entries):
                raise CassetteError(f"cassette exhausted at step {step}")
            entry = self._entries[self._cursor]
            if entry.transcript_hash != transcript_hash or entry.tools_hash != tools_hash:
                raise CassetteError(f"cassette request mismatch at step {step}")
        else:
            assert self._inner is not None
            try:
                action = self._inner.next_action(transcript, tools)
            except Exception:
                # Provider exception strings may contain request headers/keys.
                raise CassetteError(f"recording model call failed at step {step}") from None
            secret = getattr(self._inner, "api_key", None)
            response, usage = safe_response(
                action, secret=secret if isinstance(secret, str) else None
            )
            entry = CassetteEntry(
                cassette_id=self._cassette_id,
                config=self.config,
                step=step,
                transcript_hash=transcript_hash,
                tools_hash=tools_hash,
                response=response,
                usage=usage,
            )
            _action(entry)
            with self.path.open("a", encoding="utf-8") as output:
                output.write(_json(entry.model_dump(mode="json")) + "\n")
        self._cursor += 1
        return _action(entry)
