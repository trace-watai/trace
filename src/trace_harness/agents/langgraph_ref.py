"""Reference outside agent built on LangGraph.

A standard tool-calling loop as a LangGraph ``StateGraph``. An ``agent`` node
calls a chat model with the tools bound, a ``ToolNode`` runs any tool calls it
makes, and the graph ends when the model answers without one. Three pieces
connect it to the harness, and any other graph needs the same three.

- :func:`harness_tools` turns the task's tool specs into LangChain tools whose
  body is the harness ``call_tool`` callback.
- :class:`ModelResponseForwarder` is a LangChain callback handler that forwards
  every chat model response to ``on_model_response``, with reasoning blocks,
  and any text written alongside a tool call, as the reasoning.
- The final answer is the text of the last message.

The chat model here is :class:`TurnSourceChatModel`, whose turns come from a
scripted source (see ``turns.py``). Swap in any LangChain chat model to run a
real one.

``--agent trace_harness.agents.langgraph_ref:agent`` replays the committed
cassette for the task; ``:scripted_agent`` plays the task's fixture script
directly and works for every task, controls included.

Needs the ``langgraph`` extra: ``pip install -e ".[langgraph]"``.
"""

from __future__ import annotations

from typing import Any

try:
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import (
        AIMessage,
        BaseMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
        message_to_dict,
    )
    from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult
    from langchain_core.tools import BaseTool, StructuredTool
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from langgraph.graph import START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode, tools_condition
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        'the LangGraph reference agent needs the langgraph extra: pip install -e ".[langgraph]"'
    ) from exc

from pydantic import ConfigDict, Field

from trace_harness.agents.turns import (
    CassetteTurns,
    ScriptedTurns,
    TurnSource,
    assistant_message,
    observation_text,
    tool_message,
)
from trace_harness.models.base import (
    ActionKind,
    AgentAction,
    Message,
    MessageRole,
    ToolCall,
    ToolSpec,
)
from trace_harness.runner.target_agent import ModelResponseCallback, TaskPrompt, ToolCallback

NAMESPACE = "langgraph_ref"


def harness_tools(tools: list[ToolSpec], call_tool: ToolCallback) -> list[BaseTool]:
    """LangChain tools that execute through the harness.

    The JSON schema is passed through untouched and arguments are not checked
    here, so a malformed call reaches the harness and is recorded as invalid.
    """

    def make(spec: ToolSpec) -> BaseTool:
        def invoke(**arguments: Any) -> str:
            return observation_text(call_tool(spec.name, arguments))

        return StructuredTool.from_function(
            func=invoke,
            name=spec.name,
            description=spec.description,
            args_schema=spec.parameters,
        )

    return [make(spec) for spec in tools]


def reasoning_of(message: AIMessage) -> str | None:
    """The model's stated reasoning for a turn.

    Reasoning content blocks always count. Text only counts on a turn that calls
    a tool, because on the final turn the text is the answer itself.
    """
    parts = [
        str(block.get("reasoning", ""))
        for block in message.content_blocks
        if block.get("type") == "reasoning"
    ]
    if message.tool_calls and message.text:
        parts.append(message.text)
    return "\n\n".join(part for part in parts if part) or None


class ModelResponseForwarder(BaseCallbackHandler):
    """Forward every chat model response in a graph run to the harness."""

    raise_error = True

    def __init__(self, on_model_response: ModelResponseCallback) -> None:
        self.on_model_response = on_model_response

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                if isinstance(message, AIMessage):
                    self.on_model_response(message_to_dict(message), reasoning_of(message))


def reference_graph(model: BaseChatModel, tools: list[BaseTool]) -> Any:
    """The reference tool-calling loop as a compiled LangGraph graph."""
    bound = model.bind_tools(tools)

    def agent(state: MessagesState) -> dict[str, list[BaseMessage]]:
        return {"messages": [bound.invoke(state["messages"])]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    return graph.compile()


class TurnSourceChatModel(BaseChatModel):
    """A LangChain chat model whose turns come from a harness model adapter.

    The conversation is mapped back into the harness transcript shape before
    the adapter sees it, so a cassette fingerprints what the agent actually
    sent its model.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    source: Any = Field(exclude=True)

    @property
    def _llm_type(self) -> str:
        return "trace-harness-turns"

    def bind_tools(self, tools: Any, *, tool_choice: str | None = None, **kwargs: Any) -> Any:
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        specs = [
            ToolSpec(
                name=tool["function"]["name"],
                description=tool["function"].get("description", ""),
                parameters=tool["function"].get("parameters", {}),
            )
            for tool in kwargs.get("tools", [])
        ]
        action = self.source.next_action(transcript_of(messages), specs)
        message = ai_message(action, call_id=f"call_{len(messages)}")
        return ChatResult(generations=[ChatGeneration(message=message)])


def ai_message(action: AgentAction, *, call_id: str) -> AIMessage:
    """One scripted or recorded turn as the AIMessage a chat model would return."""
    content: list[str | dict[str, Any]] = []
    if action.reasoning:
        content.append({"type": "reasoning", "reasoning": action.reasoning})
    extra = {"provider_state": action.provider_state} if action.provider_state else {}
    if action.kind is ActionKind.TOOL_CALL:
        assert action.tool_call is not None
        call = {"name": action.tool_call.tool_name, "args": action.tool_call.arguments}
        return AIMessage(
            content=content, tool_calls=[{**call, "id": call_id}], additional_kwargs=extra
        )
    content.append({"type": "text", "text": action.final_answer or ""})
    return AIMessage(content=content, additional_kwargs=extra)


def transcript_of(messages: list[BaseMessage]) -> list[Message]:
    """A LangChain conversation in the harness transcript shape."""
    transcript: list[Message] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            transcript.append(Message(role=MessageRole.SYSTEM, content=message.text))
        elif isinstance(message, HumanMessage):
            transcript.append(Message(role=MessageRole.USER, content=message.text))
        elif isinstance(message, AIMessage):
            reasoning = reasoning_of(message)
            state = message.additional_kwargs.get("provider_state")
            if not message.tool_calls:
                action = AgentAction(
                    kind=ActionKind.FINAL_ANSWER,
                    final_answer=message.text,
                    reasoning=reasoning,
                    provider_state=state,
                )
                transcript.append(assistant_message(action))
            for index, call in enumerate(message.tool_calls):
                action = AgentAction(
                    kind=ActionKind.TOOL_CALL,
                    tool_call=ToolCall(tool_name=call["name"], arguments=call["args"]),
                    reasoning=reasoning if index == 0 else None,
                    provider_state=state if index == 0 else None,
                )
                transcript.append(assistant_message(action))
        elif isinstance(message, ToolMessage):
            transcript.append(tool_message(message.name or "", message.text))
    return transcript


class LangGraphReferenceAgent:
    """The reference graph as a TargetAgent."""

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
        graph = reference_graph(
            TurnSourceChatModel(source=self.turns(prompt)), harness_tools(tools, call_tool)
        )
        callbacks = []
        if self.forward_model_responses and on_model_response is not None:
            callbacks.append(ModelResponseForwarder(on_model_response))
        state = graph.invoke(
            {"messages": [SystemMessage(prompt.system), HumanMessage(prompt.user)]},
            # Each tool step is two graph steps and the answer is one more, so
            # this leaves the harness step limit as the one that binds.
            config={"callbacks": callbacks, "recursion_limit": 2 * prompt.max_steps + 2},
        )
        return state["messages"][-1].text


def agent() -> LangGraphReferenceAgent:
    """The reference agent replaying its committed cassette for the task."""
    return LangGraphReferenceAgent(CassetteTurns(NAMESPACE))


def scripted_agent() -> LangGraphReferenceAgent:
    """The reference agent playing the task's fixture script directly."""
    return LangGraphReferenceAgent(ScriptedTurns())
