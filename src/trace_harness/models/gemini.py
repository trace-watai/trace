"""GeminiModelAdapter scaffold — the first *real* model adapter, not yet live.

Why this exists now
    The team plans to prototype with free-tier Gemini API keys before
    sponsor credits land. This scaffold pins down the contract (same
    normalized :class:`AgentAction` as the fixture adapter) so the runner,
    verifiers, and dashboard never have to care which provider produced a
    trace.

Current status — deliberately not implemented
    Per the project's "no brittle half-working API code" rule,
    ``next_action`` raises :class:`NotImplementedError` with setup
    instructions instead of shipping an untested provider call.

What it should eventually do
    1. Convert the transcript (``list[Message]``) into Gemini contents.
    2. Convert each ``ToolSpec`` into a Gemini function declaration
       (``tool_mode == "native"``), or render tools into the prompt and parse
       structured JSON output (``tool_mode == "json"``).
    3. Call the model via the ``google-genai`` SDK (install with
       ``pip install -e ".[gemini]"``).
    4. Normalize the response into one ``AgentAction`` (tool call or final
       answer), stashing the provider payload in ``AgentAction.raw``.
    5. Map SDK errors to :class:`ModelAdapterError` so the runner records a
       clean ``model_error`` termination.

Out of scope here
    Retries, rate limiting, cost tracking, parallel tool calls, streaming.

Free-tier note
    Free Gemini keys are for early prototyping only; expect tight rate
    limits. The team will switch to sponsor credits if limits are hit.
    Tests and CI must keep using the fixture provider regardless.

# TODO(Rupert/runner): implement steps 1-5 above; start with tool_mode
# "native" function calling and add a transcript→contents unit test using a
# recorded SDK response (no live API call in tests).
"""

from __future__ import annotations

import os

from trace_harness.models.base import AgentAction, Message, ToolSpec

DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"


class GeminiNotConfiguredError(RuntimeError):
    """Raised at construction time when the Gemini adapter cannot be used."""


class GeminiModelAdapter:
    """Scaffolded adapter for Google Gemini models.

    Construction validates configuration (so ``--provider gemini`` fails
    fast, with instructions); ``next_action`` is the unimplemented seam.
    """

    name = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or DEFAULT_GEMINI_MODEL
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            raise GeminiNotConfiguredError(
                "GEMINI_API_KEY is not set. Get a free key from "
                "https://aistudio.google.com/apikey, put it in your local "
                ".env (see .env.example), and re-run. The fixture provider "
                "(TRACE_MODEL_PROVIDER=fixture) needs no key and is the "
                "default for all tests and CI."
            )

    def next_action(self, transcript: list[Message], tools: list[ToolSpec]) -> AgentAction:
        raise NotImplementedError(
            "GeminiModelAdapter is a scaffold: the provider call is not "
            "implemented yet (see the module docstring for the intended "
            "design). Use the fixture provider for now, or pick up the "
            "TODO(Rupert/runner) in trace_harness/models/gemini.py."
        )
