"""Reference outside agent built on the OpenAI Agents SDK.

An SDK ``Agent`` with the task's tools, run by ``Runner.run_sync``, so the
tool-calling loop, turn limit, and tool dispatch are the SDK's own. Three
pieces connect it to the harness, and any other SDK agent needs the same three.

- :func:`harness_tools` turns the task's tool specs into ``FunctionTool`` objects
  whose body is the harness ``call_tool`` callback.
- :class:`ModelResponseForwarder` is a ``RunHooks`` whose ``on_llm_end`` forwards
  every model response to ``on_model_response``, with reasoning summaries, and
  any message text written alongside a tool call, as the reasoning.
- The final answer is ``RunResult.final_output``.

SDK tracing is switched off for every run, so nothing is exported anywhere.

The model here is :class:`TurnSourceModel`, whose turns come from a scripted
source (see ``turns.py``). Swap in any SDK ``Model`` to run a real one.
Opaque provider state such as a Gemini thought signature is not carried
through this model, so recording a model that needs it echoed back would need
that added first.

``--agent trace_harness.agents.openai_agents_ref:agent`` replays the committed
cassette for the task; ``:scripted_agent`` plays the task's fixture script
directly and works for every task, controls included.

Needs the ``openai-agents`` extra: ``pip install -e ".[openai-agents]"``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

try:
    from agents import (
        Agent,
        FunctionTool,
        Model,
        ModelResponse,
        RunConfig,
        RunHooks,
        Runner,
        Usage,
    )
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
        ResponseReasoningItem,
    )
    from openai.types.responses.response_reasoning_item import Summary
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "the OpenAI Agents SDK reference agent needs the openai-agents extra: "
        'pip install -e ".[openai-agents]"'
    ) from exc

from trace_harness.agents.turns import (
    CassetteTurns,
    ScriptedTurns,
    TurnSource,
    assistant_message,
    observation_text,
    tool_arguments,
    tool_message,
)
from trace_harness.models.base import (
    ActionKind,
    AgentAction,
    Message,
    MessageRole,
    ModelAdapter,
    ToolCall,
    ToolSpec,
)
from trace_harness.runner.target_agent import ModelResponseCallback, TaskPrompt, ToolCallback

NAMESPACE = "openai_agents_ref"


def harness_tools(tools: list[ToolSpec], call_tool: ToolCallback) -> list[FunctionTool]:
    """SDK function tools that execute through the harness.

    The schema is not made strict and arguments are not checked here, so a
    malformed call reaches the harness and is recorded as invalid.
    """

    def make(spec: ToolSpec) -> FunctionTool:
        async def invoke(context: Any, arguments: str) -> str:
            observation = await asyncio.to_thread(call_tool, spec.name, tool_arguments(arguments))
            return observation_text(observation)

        return FunctionTool(
            name=spec.name,
            description=spec.description,
            params_json_schema=spec.parameters,
            on_invoke_tool=invoke,
            strict_json_schema=False,
        )

    return [make(spec) for spec in tools]


def _item(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else value.model_dump(mode="json", exclude_none=True)


def _text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    return "".join(
        str(part.get("text", ""))
        for part in content or []
        if isinstance(part, dict) and part.get("type") in ("output_text", "input_text", "text")
    )


def reasoning_of(output: list[Any]) -> str | None:
    """The model's stated reasoning for one response.

    Reasoning summaries always count. Message text only counts in a response
    that also calls a tool, because otherwise the text is the answer itself.
    """
    items = [_item(item) for item in output]
    parts = [
        str(summary.get("text", ""))
        for item in items
        if item.get("type") == "reasoning"
        for summary in item.get("summary") or []
    ]
    if any(item.get("type") == "function_call" for item in items):
        parts += [_text(item) for item in items if item.get("type") == "message"]
    return "\n\n".join(part for part in parts if part) or None


class ModelResponseForwarder(RunHooks):
    """Forward every model response in an SDK run to the harness."""

    def __init__(self, on_model_response: ModelResponseCallback) -> None:
        self.on_model_response = on_model_response

    async def on_llm_end(self, context: Any, agent: Any, response: ModelResponse) -> None:
        raw = {
            "response_id": response.response_id,
            "output": [_item(item) for item in response.output],
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        }
        self.on_model_response(raw, reasoning_of(response.output))


def output_items(action: AgentAction, *, turn: int) -> list[Any]:
    """One scripted or recorded turn as the output items a model would return."""
    items: list[Any] = []
    if action.reasoning:
        items.append(
            ResponseReasoningItem(
                id=f"rs_{turn}",
                summary=[Summary(text=action.reasoning, type="summary_text")],
                type="reasoning",
            )
        )
    if action.kind is ActionKind.TOOL_CALL:
        assert action.tool_call is not None
        items.append(
            ResponseFunctionToolCall(
                id=f"fc_{turn}",
                call_id=f"call_{turn}",
                name=action.tool_call.tool_name,
                arguments=json.dumps(action.tool_call.arguments),
                type="function_call",
                status="completed",
            )
        )
    else:
        items.append(
            ResponseOutputMessage(
                id=f"msg_{turn}",
                content=[
                    ResponseOutputText(
                        annotations=[], text=action.final_answer or "", type="output_text"
                    )
                ],
                role="assistant",
                status="completed",
                type="message",
            )
        )
    return items


def transcript_of(system_instructions: str | None, input: str | list[Any]) -> list[Message]:
    """An SDK model request in the harness transcript shape."""
    transcript: list[Message] = []
    if system_instructions:
        transcript.append(Message(role=MessageRole.SYSTEM, content=system_instructions))
    items = [{"role": "user", "content": input}] if isinstance(input, str) else input
    thinking: list[str] = []
    said: list[str] = []
    tool_names: dict[str, str] = {}
    for raw in items:
        item = _item(raw)
        kind = item.get("type", "message")
        if kind == "message" and item.get("role") == "user":
            transcript.append(Message(role=MessageRole.USER, content=_text(item)))
        elif kind == "reasoning":
            thinking += [str(s.get("text", "")) for s in item.get("summary") or []]
        elif kind == "message":
            said.append(_text(item))
        elif kind == "function_call":
            tool_names[item["call_id"]] = item["name"]
            reasoning = "\n\n".join(part for part in thinking + said if part) or None
            thinking, said = [], []
            call = ToolCall(tool_name=item["name"], arguments=tool_arguments(item["arguments"]))
            action = AgentAction(kind=ActionKind.TOOL_CALL, tool_call=call, reasoning=reasoning)
            transcript.append(assistant_message(action))
        elif kind == "function_call_output":
            name = tool_names.get(item.get("call_id", ""), "")
            transcript.append(tool_message(name, str(item.get("output", ""))))
    if said:
        action = AgentAction(
            kind=ActionKind.FINAL_ANSWER,
            final_answer="".join(said),
            reasoning="\n\n".join(part for part in thinking if part) or None,
        )
        transcript.append(assistant_message(action))
    return transcript


class TurnSourceModel(Model):
    """An Agents SDK model whose turns come from a harness model adapter.

    The request is mapped back into the harness transcript shape before the
    adapter sees it, so a cassette fingerprints what the agent actually sent.
    """

    def __init__(self, source: ModelAdapter) -> None:
        self.source = source

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[Any],
        model_settings: Any,
        tools: list[Any],
        output_schema: Any,
        handoffs: list[Any],
        tracing: Any,
        *,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any = None,
    ) -> ModelResponse:
        specs = [
            ToolSpec(name=t.name, description=t.description, parameters=t.params_json_schema)
            for t in tools
            if isinstance(t, FunctionTool)
        ]
        transcript = transcript_of(system_instructions, input)
        action = self.source.next_action(transcript, specs)
        turn = len(input) if isinstance(input, list) else 1
        output = output_items(action, turn=turn)
        return ModelResponse(output=output, usage=Usage(), response_id=None)

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("the reference model does not stream")


class OpenAIAgentsReferenceAgent:
    """An Agents SDK agent as a TargetAgent."""

    def __init__(self, turns: TurnSource, *, forward_model_responses: bool = True) -> None:
        self.turns = turns
        self.forward_model_responses = forward_model_responses
        self.name = f"{NAMESPACE}:{getattr(turns, 'label', 'custom')}"

    def run(
        self,
        prompt: TaskPrompt,
        tools: list[ToolSpec],
        call_tool: ToolCallback,
        on_model_response: ModelResponseCallback | None = None,
    ) -> str:
        agent = Agent(
            name="trace_harness_reference",
            instructions=prompt.system,
            tools=harness_tools(tools, call_tool),
            model=TurnSourceModel(self.turns(prompt)),
        )
        hooks = None
        if self.forward_model_responses and on_model_response is not None:
            hooks = ModelResponseForwarder(on_model_response)
        result = Runner.run_sync(
            agent,
            prompt.user,
            # One model turn per tool step plus the answer, so the harness step
            # limit is the one that binds.
            max_turns=prompt.max_steps + 1,
            hooks=hooks,
            run_config=RunConfig(tracing_disabled=True),
        )
        return result.final_output


def agent() -> OpenAIAgentsReferenceAgent:
    """The reference agent replaying its committed cassette for the task."""
    return OpenAIAgentsReferenceAgent(CassetteTurns(NAMESPACE))


def scripted_agent() -> OpenAIAgentsReferenceAgent:
    """The reference agent playing the task's fixture script directly."""
    return OpenAIAgentsReferenceAgent(ScriptedTurns())
