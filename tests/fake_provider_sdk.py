"""Stand-in ``anthropic`` and ``openai`` modules for the live adapters' tests.

The adapters import their SDK when they are built and call it on every turn.
These fakes go into ``sys.modules`` under the real names, so the adapter code
runs unchanged: it builds its client through the module, sends each request to
a scripted endpoint that keeps every request it was given, and maps the
module's error class to a model error. No package is installed, no key is read
and nothing touches the network, so the suite still passes with both SDKs
blocked.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest


class APIError(Exception):
    """Named after ``anthropic.APIError``, the base the call policy knows by name."""


class OpenAIError(Exception):
    """Named after ``openai.OpenAIError``, the base the call policy knows by name."""


class FakeSDKError(APIError, OpenAIError):
    """What either fake SDK raises, standing in for an SDK error with no status.

    The shared call policy (#196) recognizes a provider's own error by the
    class name of the SDK's base, so this carries both names. With no status
    it is permanent: one attempt, then a model error.
    """


@dataclass
class ScriptedEndpoint:
    """A ``create`` method that answers from a script and keeps each request."""

    outcomes: list[Any] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, **request: Any) -> Any:
        self.requests.append(request)
        if not self.outcomes:
            raise AssertionError("the fake SDK was called more often than scripted")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@dataclass
class FakeSDK:
    """One installed fake module, what built its client, and its endpoint."""

    endpoint: ScriptedEndpoint
    client_kwargs: dict[str, Any] = field(default_factory=dict)

    def script(self, *outcomes: Any) -> None:
        self.endpoint.outcomes.extend(outcomes)

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.endpoint.requests


def install_anthropic(monkeypatch: pytest.MonkeyPatch) -> FakeSDK:
    sdk = FakeSDK(ScriptedEndpoint())

    def client(**kwargs: Any) -> SimpleNamespace:
        sdk.client_kwargs.update(kwargs)
        return SimpleNamespace(messages=SimpleNamespace(create=sdk.endpoint))

    module = SimpleNamespace(Anthropic=client, APIError=APIError)
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return sdk


def install_openai(monkeypatch: pytest.MonkeyPatch) -> FakeSDK:
    sdk = FakeSDK(ScriptedEndpoint())

    def client(**kwargs: Any) -> SimpleNamespace:
        sdk.client_kwargs.update(kwargs)
        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=sdk.endpoint))
        )

    module = SimpleNamespace(OpenAI=client, OpenAIError=OpenAIError)
    monkeypatch.setitem(sys.modules, "openai", module)
    return sdk


def block_sdk(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make ``import name`` fail, the way it does when the package is absent."""
    monkeypatch.setitem(sys.modules, name, None)
