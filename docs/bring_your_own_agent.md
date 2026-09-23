# Bring your own agent

The harness can run an agent it did not build. Your agent keeps its own loop
and its own model calls. The harness keeps the refund environment, the trace,
the verifier, and everything downstream of them, so a run of your agent gets
the same audit as a run of ours. That means a verdict, attribution, a failure
card, a repair package, and a regression artifact that replays without your
agent.

The code lives in `src/trace_harness/runner/target_agent.py`.

## The contract

An outside agent is any object with a `name` and a `run` method.

```python
class TargetAgent(Protocol):
    name: str

    def run(
        self,
        prompt: TaskPrompt,
        tools: list[ToolSpec],
        call_tool: ToolCallback,
        on_model_response: ModelResponseCallback | None = None,
    ) -> str: ...
```

- `prompt` carries `task_id`, `system`, `user`, and `max_steps`. The system and
  user text are exactly what the harness gives its own model adapters.
  `max_steps` is the harness step limit, so set your framework's own recursion
  or turn limit above it and let the harness limit be the one that binds.
- `tools` lists the task's tools as `ToolSpec` objects, each with a name, a
  description, and a JSON schema for its arguments.
- `call_tool(name, arguments)` runs one tool call inside the harness and blocks
  until it has. It returns a `ToolObservation` with `tool_name`, `status`
  (`ok` or `error`), `result`, and `error`. Give that to your model the way you
  would give it any tool result.
- `on_model_response(raw, reasoning=None)` forwards one model response. Call it
  once per response, before acting on it. `raw` is a JSON object describing
  the response and `reasoning` is any text the model gave for its decision.
  The harness stores `raw` in `trace.jsonl` as given, so leave out anything you
  would not want written to a file.
- Return the final answer to the customer as a string.
- `name` becomes the run's `model` label in `run_config.json` and the run
  index, so make it say what ran, for example `my-graph:gpt-5`.

A complete agent can be this small.

```python
class EchoAgent:
    name = "echo"

    def run(self, prompt, tools, call_tool, on_model_response=None):
        order = call_tool("get_order", {"customer_name": "Riley Chen"})
        return f"Your order lookup came back {order.status}."
```

## Running it

Point `--agent` at a `package.module:attribute` import path. The attribute can
be an agent instance, a class, or a function that takes no arguments and
returns an agent.

```sh
trace-harness run-pipeline fixtures/tasks/refund_policy_failure.json \
  --agent mypackage.agents:make_agent
```

`run-fixture` takes the same flag. In a suite manifest, the agent config is

```json
{"label": "my-graph", "provider": "external", "agent_ref": "mypackage.agents:make_agent"}
```

with an optional `model` that overrides the agent's own label. `run_config.json`
(RunConfig 0.4.0) records `provider: external`, the `agent_ref`, and the label.
Suite manifests that name an outside agent are Suite 0.4.0.
`--max-steps` and `--timeout` apply as usual. `--script`, `--cassette-mode`,
`--temperature`, and `--seed` are refused with `--agent`, because the outside
agent owns its model and the harness would be recording settings it never
applied.

## What the harness guarantees

The bridge hands each of your agent's moves to the same `AgentRunner` loop the
built-in adapters use, so none of the following is reimplemented for outside
agents.

- **Steps.** Each `call_tool` is one step and the final answer is one more,
  numbered from 1. Every event a step causes carries its step id, which is what
  verifier checks and attribution point at.
- **Validation.** A call to an unknown tool or with arguments that do not match
  the schema is recorded as `tool_call_validated` with `valid: false` and is
  never executed. Your agent gets the validation error as the observation and
  can recover.
- **Controls.** Controls installed through `install_control` run at the
  pre-call seam before any handler. A blocked call returns `status: error` with
  the control's message, and the trace records the control id as `blocked_by`
  on both `tool_call_executed` and `tool_observation`. Final-answer controls run
  on the answer you return, and a blocked answer ends the run as
  `final_answer_blocked`.
- **Limits.** The step limit and the timeout are enforced by the harness. When
  a run ends early, the call your agent is waiting on and every later
  `call_tool` raise `RunEnded`. The harness cannot stop your agent's thread,
  but nothing it does after that point reaches the environment or the trace.
- **Parallel calls.** Tool calls made from several threads at once are taken in
  arrival order as consecutive steps, and each caller gets the result of its
  own call.
- **Failures.** An exception out of `run` ends the run as `model_error` with the
  exception type and message in the trace. So does returning something other
  than a string. A `ScriptExhaustedError` from a scripted model keeps its own
  `script_exhausted` reason.
- **Regressions.** Every move is recorded as a `model_action`, so a failure's
  regression artifact pins your agent's moves and `trace-harness replay`
  reproduces the failure offline without your agent installed.

## What the model callback adds

The callback is optional, and leaving it out changes what attribution can say.

With it wired, every forwarded response becomes a `model_response` event at the
step of the move that followed it, and its `reasoning` lands on that step's
`model_action`. Attribution can then find a root cause the agent stated in its
own words, such as committing to a deprecated policy document, and the verifier
can see those citations too.

Without it, the trace still has one `model_action` per move, with `reasoning`
null and no `model_response` events. Attribution degrades to nulls. The fields
that depend on reasoning stay empty and `ambiguity_notes` says the trace
exposes no model reasoning, while the fields that tool calls and final state
support are still filled. On the staged refund failure the difference looks
like this.

| field | callback wired | callback not wired |
|---|---|---|
| `root_cause_step` | 3 | null |
| `first_bad_step` | 3 | 5 |
| `missed_recovery_step` | 4 | 4 |
| `first_irreversible_action_step` | 5 | 5 |

The null is deliberate. Without reasoning, the only other candidate is the
unsupported ticket claim at step 6. It comes after the unauthorized refund at
step 5 and cannot be what caused it, so the attributor leaves the root cause
empty and notes the earlier step.

## Retries, time, cost, and branching

- **Retries.** The harness sends no model request for your agent, so the
  retry and rate-limit policy its own live adapters use (#196) never applies.
  `run_config.json` records `call_policy` as null, as it does for fixture and
  replay runs, and a suite agent config with `provider: external` refuses a
  `call_policy`. If your model calls need retries, your agent makes them. An
  exception out of your agent ends the run at once as `model_error` and is
  never retried.
- **Time.** `--timeout`, or `timeout_seconds` in a suite, bounds the whole run.
  Each move gets what is left of it, and a move includes every model call your
  agent makes before it, so a slow or retried model call spends the run's
  time.
- **Cost.** The harness cannot see what your agent's model calls cost, so a
  batch entry for your agent records `cost_usd` as null. A suite with
  `max_cost_usd` therefore refuses your agent config before its first run as
  `budget_unenforceable`, and `run-suite` exits 2, the same as for a live model
  with no price. Without a cap the config runs like any other.
- **Branching.** `trace-harness branch` does not run outside agents yet. A
  condition whose agent config has `provider: external` is refused before any
  run with an error that says so, and `branch` takes no `--agent` flag.

## Reference agents

Working examples ship under `src/trace_harness/agents/`, each behind its own
extra. The core package never imports them, and their tests skip when the
extra is not installed.

The model underneath every reference agent is scripted. Its turns come from
the task's fixture script, either directly or through a cassette recorded from
that script, so a reference run shows the outside-agent path working end to end
and says nothing about how a live model behaves. The agent's `name`, and so the
`model` field of every run it produces, says which source was used, for example
`langgraph_ref:cassette:scripted`.

Each reference module has two factories.

- `:agent` replays the committed cassette for the task. Cassettes are committed
  for `refund_policy_valid_cash` and `refund_policy_failure`. Replay checks
  every request the agent sends its model against the recording, so a run that
  drifts from it, for example because a control blocked a call the recording
  saw succeed, stops with a request mismatch at the next step.
- `:scripted_agent` plays the task's fixture script directly and ignores what
  the model is sent. It works for every task, with or without controls.

The cassettes use the harness model cassette format described in
`fixtures/cassettes/README.md`, and `scripts/record_reference_cassettes.py`
reproduces them. `CassetteTurns(mode="record", inner=...)` records around any
harness model adapter, so a real model can be recorded once with credentials
and then replayed offline by the same tests. Only the scripted source has been
recorded so far.

### LangGraph

```sh
pip install -e ".[langgraph]"
trace-harness run-pipeline fixtures/tasks/refund_policy_failure.json \
  --agent trace_harness.agents.langgraph_ref:agent
```

`langgraph_ref.py` builds a tool-calling loop as a `StateGraph`. An `agent` node
calls the chat model with the tools bound, a `ToolNode` runs the calls, and the
graph ends when the model answers without one. Any graph connects to the
harness with the same three pieces.

- `harness_tools(tools, call_tool)` returns LangChain tools whose body is the
  harness callback. Arguments are passed through unchecked so the harness
  records malformed calls as invalid.
- `ModelResponseForwarder(on_model_response)` is a LangChain callback handler.
  Pass it in the graph's `config={"callbacks": [...]}` and every chat model
  response is forwarded. Reasoning content blocks, and any text written next to
  a tool call, become the reasoning.
- The final answer is the text of the last message.

Set the graph's `recursion_limit` to at least `2 * max_steps + 1`, because each
tool step is two graph steps and the answer is one more. The reference agent
uses `2 * max_steps + 2`, which leaves the harness step limit as the one that
binds.

## Out of scope

Hosting outside agents as a service, and adapters for browser or coding
agents.
