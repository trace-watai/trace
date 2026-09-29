# Reference outside agents on a scripted model, 23 September 2026

Two runs of `refund_policy_failure` through the bring-your-own-agent path
(#210), one per reference adapter. Each carries the complete chain, from the
verifier result through attribution, the failure card, the repair package, and
the regression artifact.

| run | agent | model label | verdict | root cause step |
|---|---|---|---|---|
| `run_20260923T073535Z_886164c5` | LangGraph, `trace_harness.agents.langgraph_ref:agent` | `langgraph_ref:cassette:scripted` | fail | 3 |
| `run_20260923T073538Z_08b09806` | OpenAI Agents SDK, `trace_harness.agents.openai_agents_ref:agent` | `openai_agents_ref:cassette:scripted` | fail | 3 |

**The model behind both runs is scripted.** The framework loops are real, a
LangGraph `StateGraph` in one and an Agents SDK `Runner` in the other, but every
model turn was replayed from a cassette recorded from
`fixtures/scripts/refund_policy_failure_script.json`. The failure is that
script's staged failure. These runs say nothing about how a live model behaves
under either framework and must not be quoted as live-agent evidence.

What they do show is the outside-agent path working end to end. The agent owned
its loop and reached the environment only through `call_tool`. It forwarded
every model response through `on_model_response`, so each of the seven steps
has a `model_response` event and the forwarded reasoning on its `model_action`,
which is what lets attribution place the root cause at step 3. The regression
artifacts pin the agent's moves, so `collect-regressions docs/acceptance/runs`
replays both runs offline with neither SDK installed, and each `replay_command`
names the agent that produced the run.

Regenerate them from the repository root with both extras installed. Run ids,
timestamps, and the message ids LangChain generates will differ.

```sh
trace-harness run-pipeline fixtures/tasks/refund_policy_failure.json \
  --agent trace_harness.agents.langgraph_ref:agent
trace-harness run-pipeline fixtures/tasks/refund_policy_failure.json \
  --agent trace_harness.agents.openai_agents_ref:agent
```

Each `run_config.json` is RunConfig 0.4.0, with `agent_ref` set and
`call_policy` null because the harness made no model call for the outside
agent. Each `attribution_result.json` is Attribution 0.4.0, with `block_step`
null and `post_block_outcome` `no_block_observed` because no control was
installed.

No network call was made to produce them, and no key or auth header appears in
any file here.
