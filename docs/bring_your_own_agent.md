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

- `prompt` carries `task_id`, `system`, `user`, `max_steps`, and
  `timeout_seconds`. The system and user text are exactly what the harness
  gives its own model adapters. `max_steps` is the harness step limit, so set
  your framework's own recursion or turn limit above it and let the harness
  limit be the one that binds. `timeout_seconds` is the run's time limit, or
  null when there is none. The harness cannot stop your agent's thread, so an
  agent that starts processes of its own can use it to stop them once the run
  is over.
- `tools` lists the task's tools as `ToolSpec` objects, each with a name, a
  description, and a JSON schema for its arguments.
- `call_tool(name, arguments)` runs one tool call inside the harness and blocks
  until it has. It returns a `ToolObservation` with `tool_name`, `status`
  (`ok` or `error`), `result`, and `error`. Give that to your model the way you
  would give it any tool result.
- `on_model_response(raw, reasoning=None)` forwards one model response. Call it
  once per response, before acting on it. `raw` is a JSON object describing
  the response and `reasoning` is any text the model gave for its decision.
  The harness writes `raw` to `trace.jsonl` as a JSON round trip, so leave out
  anything you would not want written to a file. A value JSON cannot hold is
  stored as its string form, a `raw` that is not a dict is stored as
  `{"response": raw}`, and when several responses arrive before one move they
  are stored together as `{"responses": [...]}`.
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
applied. A suite agent config with `provider: external` refuses `cassette`,
`call_policy`, `temperature`, and `seed` for the same reason.

## What the harness guarantees

The bridge hands each of your agent's moves to the same `AgentRunner` loop the
built-in adapters use, so none of the following is reimplemented for outside
agents.

- **Steps.** Each `call_tool` is one step and the final answer is one more,
  numbered from 1. Every event a step causes carries its step id, which is what
  verifier checks and attribution point at.
- **Validation.** A `call_tool` that names an unknown tool, or whose arguments
  do not match the schema, is recorded as `tool_call_validated` with
  `valid: false` and is never executed. Your agent gets the validation error as
  the observation and can recover. A framework may catch an unknown tool name
  before it ever reaches `call_tool`, and then the harness records no tool call
  for it. Under the reference agents, LangGraph's `ToolNode` answers the model
  with its own error and the graph goes on, and the Agents SDK raises
  `ModelBehaviorError`, which ends the run as `model_error`. Either way the
  forwarded model response that named the tool is still in the trace.
  Arguments that miss the schema reach the harness under both.
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
  `script_exhausted` reason. Model responses forwarded before the exception
  are recorded as a `model_response` at that step, ahead of the `error` event.
- **Regressions.** Every move is recorded as a `model_action`, so a failure's
  regression artifact pins your agent's moves and `trace-harness replay`
  reproduces the failure offline without your agent installed.

## What the model callback adds

The callback is optional, and leaving it out changes what attribution can say.

With it wired, every forwarded response becomes a `model_response` event at the
step of the move that followed it, and its `reasoning` lands on that step's
`model_action`. A response followed by an exception instead of a move is
recorded at the step the exception ended. Attribution can then find a root cause the agent stated in its
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

The model underneath the LangGraph and Agents SDK references is scripted. Its
turns come from the task's fixture script, either directly or through a
cassette recorded from that script, so a run of either shows the outside-agent
path working end to end and says nothing about how a live model behaves. The
agent's `name`, and so the `model` field of every run it produces, says which
source was used, for example `langgraph_ref:cassette:scripted`. The Claude Code
agent runs a live model and has [its own section](#claude-code).

Each reference module has two factories.

- `:agent` replays the committed cassette for the task. Cassettes are committed
  for `refund_policy_valid_cash` and `refund_policy_failure`. Replay checks
  every request the agent sends its model against the recording, so a run that
  drifts from it, for example because a control blocked a call the recording
  saw succeed, stops with a request mismatch at the next step.
- `:scripted_agent` plays the task's fixture script directly and ignores what
  the model is sent. The script is the one the task's `metadata.fixture_script`
  names, the same file the fixture provider plays, found by task id under
  `fixtures/tasks/`. It works for every task, with or without controls.

Both factories find the committed cassettes and task fixtures from where the
package sits in the repository, so they run the same from any working
directory. They need the source checkout (an editable install), since the
fixtures are not part of the package.

The cassettes use the harness model cassette format described in
`fixtures/cassettes/README.md`, and `scripts/record_reference_cassettes.py`
reproduces them. `CassetteTurns(mode="record", inner=...)` records around any
harness model adapter, so a real model can be recorded once with credentials
and then replayed offline through the same agent code. Only the scripted source
has been recorded so far.

One run of `refund_policy_failure` per reference agent is retained, with its
full failure bundle, under
`docs/acceptance/runs/reference-agents-scripted-2026-09-23/`. Its README says
plainly that the model was scripted.

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

### OpenAI Agents SDK

```sh
pip install -e ".[openai-agents]"
trace-harness run-pipeline fixtures/tasks/refund_policy_failure.json \
  --agent trace_harness.agents.openai_agents_ref:agent
```

The extra names `openai>=3,<4` beside the SDK, because the reference imports
openai's response types directly and openai-agents 0.22 requires openai 3. The
`openai` extra takes the same range since #160, so every extra installs
together, and a test checks the ranges in `pyproject.toml` still allow it.

`openai_agents_ref.py` builds an SDK `Agent` with the task's tools and runs it
with `Runner.run` under `asyncio.run`, so the loop, the turn limit, and tool
dispatch are the SDK's own. `Runner.run_sync` would leave its event loop open on
the agent's thread, and with it the executor threads the tools call `call_tool`
from, until garbage collection. `asyncio.run` closes the loop and joins them
before the run returns. Any SDK agent connects to the harness with the same
three pieces.

- `harness_tools(tools, call_tool)` returns `FunctionTool` objects whose body
  is the harness callback. The schemas are not made strict and arguments are
  passed through unchecked, so the harness records malformed calls as invalid.
- `ModelResponseForwarder(on_model_response)` is a `RunHooks`. Pass it as
  `hooks=` and every model response is forwarded from `on_llm_end`. Reasoning
  summaries, and any message text written next to a tool call, become the
  reasoning.
- The final answer is `RunResult.final_output`.

Set `max_turns` to at least `max_steps + 1`, one model turn per tool step plus
the answer. Pass `RunConfig(tracing_disabled=True)` unless you want the SDK to
export its own traces. The reference agent always passes it, and its tests
check that no SDK trace is started.

The reference model carries opaque provider state, such as a Gemini thought
signature or Anthropic's tool-use id and thinking blocks, through the SDK's
items. `output_items` keeps it in the turn's reasoning item, in the
`encrypted_content` field the SDK replays unchanged, and `transcript_of` puts
it back on the harness turn for the next request. The LangGraph reference
carries it in the message's `additional_kwargs`.

## Claude Code

`trace_harness.agents.claude_code_ref` runs the task through the local Claude
Code CLI. Claude Code's own agent loop drives the model, and the harness keeps
the environment, the controls, the trace, and the verifier as it does for any
outside agent. The CLI's model calls run on the Claude plan it is logged in
with, so a run counts against that plan's usage and carries no per-token
charge.

### Setup

Install Claude Code ([setup](https://code.claude.com/docs/en/setup)) and log in
once in a terminal by running `claude` and `/login` with the Claude account
whose plan should carry the runs. The agent was built against version 2.1.273.
Nothing else is needed, since the module uses only the core package.

```sh
trace-harness run-pipeline fixtures/tasks/refund_policy_valid_cash.json \
  --agent trace_harness.agents.claude_code_ref:agent
```

`:agent` runs `claude-sonnet-5`, and the run's `model` field is
`claude_code_ref:claude-sonnet-5`. Another model, or a recording, takes a small
factory of your own.

```python
from pathlib import Path

from trace_harness.agents.claude_code_ref import ClaudeCodeAgent, ClaudeCodeCassette


def recording_agent():
    return ClaudeCodeAgent(
        "claude-sonnet-5",
        cassette=ClaudeCodeCassette(Path("/tmp/claude-code-cassettes"), "record"),
    )
```

The agent never logs in and never answers a prompt of the CLI. A CLI that is
not logged in ends the run with an error saying so.

### What it guarantees

Each run starts `claude -p` in a fresh temporary directory, which is removed
afterwards. The flags are described in the
[CLI reference](https://code.claude.com/docs/en/cli-reference) and
[headless mode](https://code.claude.com/docs/en/headless).

- **Only the task's tools.** `--tools ""` removes every built-in tool. The only
  tool source is a small MCP server in the package
  (`agents/claude_code_mcp.py`), loaded through `--mcp-config` with
  `--strict-mcp-config`, which lists exactly the task's tools. The CLI names
  them `mcp__trace__<tool>`. Tool search is turned off, so no search tool is
  added. The run's `system/init` message must list exactly those tools, or the
  run ends before any tool call.
- **Every call goes through the harness.** The MCP server relays each call over
  a Unix socket to the agent, which runs it through `call_tool`. Validation,
  controls, `blocked_by`, the step and time limits, and the trace work
  unchanged. A blocked call reaches the CLI as the control's message, as an
  MCP tool result with `isError` set.
- **Nothing waits on a person.** Only the task's tools are allowed, under
  `--permission-mode dontAsk` and `--permission-prompts none`, so anything
  that would prompt is denied.
- **Nothing else shapes the run.** `--setting-sources ""` loads no user,
  project, or local settings, so no hooks, permission rules, or CLAUDE.md
  files apply. Auto memory and claude.ai connectors are off, and
  `--no-session-persistence` saves no session.
- **The prompt is the harness's.** `--system-prompt` is the task's system
  prompt with one line added that says how the CLI names the tools. The task's
  user message is the first message, sent on stdin.
- **The plan pays.** The CLI's environment has no `ANTHROPIC_API_KEY` or
  `ANTHROPIC_AUTH_TOKEN`, which would otherwise take precedence over the
  login ([environment variables](https://code.claude.com/docs/en/env-vars)),
  and no variable that marks a nested Claude Code session. The `system/init`
  message must report `apiKeySource` as `none`, or the run ends before any
  tool call.
- **The model asked for.** The `system/init` message and every model response
  must name the model the agent was given. A fallback to another model ends
  the run.
- **Reasoning reaches attribution.** Every assistant message in the stream is
  forwarded to `on_model_response`. The CLI writes one message per content
  block, so the blocks of one model response are forwarded together, with
  thinking signatures left out. Thinking, and any text written beside a tool
  call, become the step's reasoning. A tool call waits until its `tool_use`
  block has been read, so the response that made the call lands on the call's
  step. The `result` message's `usage`, `modelUsage`, and `total_cost_usd` are
  forwarded last, with the CLI version, at the final step.
- **Failures end cleanly.** No `claude` on PATH, a missing login, an error
  result, a non-zero exit, a stream line that is not JSON, a rejected usage
  limit, and a run past its time limit each end the run as `model_error` with
  a message that says which. The CLI runs in its own process group, which is
  stopped with the MCP server inside it when the run ends, when the harness
  run ended first (the next tool call gets `RunEnded`, or the run's time limit
  plus 5 seconds passes), and when the interpreter exits.
- **Offline replay.** With a `ClaudeCodeCassette` in `record` mode, every move
  is written to a harness model cassette at
  `<root>/claude_code_ref/<task_id>/<model>/default.jsonl`, with the responses
  forwarded for it. In `replay` mode no CLI starts. Each move is served from
  the cassette after the conversation so far is checked against the
  recording, so a replay forwards the same responses and makes the same calls,
  and a run that drifts from the recording stops with a request mismatch.
  Recording never overwrites a cassette.

`tests/test_claude_code_agent.py` checks each of these with a fake `claude` on
PATH (`tests/fake_claude_cli.py`) that starts the MCP server the way the CLI
does. No test runs the real CLI.

### Rate limits and your plan

- A run uses the usage limits of the plan the CLI is logged in with, the same
  limits that claude.ai and every other Claude Code session draw on
  ([costs](https://code.claude.com/docs/en/costs)).
- When the CLI reports a `rate_limit_event` with status `rejected`, the plan's
  limit is reached. The agent stops the CLI at once and the run ends as
  `model_error`, with the time the limit resets. Under `branch` that run is
  incomplete, so a seed replacement is spent on it.
- With usage credits turned on, use past the plan's limit is charged to those
  credits, and the harness cannot see that charge. Turn them off if the plan
  alone should carry the runs.
- `total_cost_usd` is the CLI's client-side estimate at API list prices
  ([cost tracking](https://code.claude.com/docs/en/agent-sdk/cost-tracking)).
  For a run on a plan it is what the same calls would have cost over the API,
  and nothing was charged.

## Out of scope

Hosting outside agents as a service, and adapters for browser or coding
agents.
