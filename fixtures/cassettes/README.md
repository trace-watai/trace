# Model cassettes

Run the retained Gemini example offline, from the repository root:

```sh
python -m trace_harness.cli run-suite fixtures/suites/refund_policy_gemini_replay.json --fail-on-verifier
```

To record a new live run into a separate directory, then replay it:

```sh
python -m trace_harness.cli run-pipeline fixtures/tasks/refund_policy_failure.json \
  --provider gemini --temperature 0 --seed 7 --timeout 120 \
  --cassette-mode record --cassette-dir /tmp/trace-cassettes-new
python -m trace_harness.cli run-pipeline fixtures/tasks/refund_policy_failure.json \
  --provider gemini --temperature 0 --seed 7 --timeout 120 \
  --cassette-mode replay --cassette-dir /tmp/trace-cassettes-new
```

Recording requires the selected provider's credentials; replay never constructs
the provider. A live recording runs under the call policy (retries, backoff,
rate limit), and each entry keeps that step's `call_record`, so the replay
shows the same attempts and delays without sleeping or calling anything. Use a new directory for a different recording: existing files are
never overwritten. Missing files, exhausted recordings, unknown schema versions,
and request/configuration mismatches are errors. A run interrupted while recording
may leave a partial cassette; it is not evidence of a completed run.

## Retained fixture provenance

`refund_policy_failure/gemini-3.6-flash/default.jsonl` contains five normalized
actions from the real Gemini run `run_20260913T141412Z_3e4b44a9`, retained in
[the live acceptance artifacts](../../docs/acceptance/live-gemini-2026-09-13/).
That run correctly declined an immediate refund and escalated; its verifier
verdict is **PASS**, despite the task's `failure` name. Model, temperature, seed,
timeout, prompt version, actions, continuation signatures, and token counts come
from those artifacts. No new live call was made to create this cassette.

This is an import of historical evidence, not a newly captured recording. Old
traces did not retain full tool declarations: their hash is reconstructed from
the current registry. Tests verify all recorded requests match a current run,
the final state and verifier findings match the original, and two independent
replays produce identical traces after excluding fresh `run_id` and `timestamp`
fields. The original verifier's older schema version is also excluded from the
verifier comparison; its verdict, checks, warnings, evidence, and release gate
are compared. Audit traces themselves keep their true run IDs and times.

Reproduce the import into a new directory:

```sh
python scripts/import_model_cassette.py \
  docs/acceptance/live-gemini-2026-09-13/run_20260913T141412Z_3e4b44a9 \
  --cassette-dir /tmp/trace-import-check
```

The full 32-task `refund_v0` suite is tested by recording fixture adapters into
temporary cassettes, then replaying twice with sockets and provider construction
forbidden. All 18 expected passes and 14 expected failures must stay identical.
These temporary suite recordings are distinct from the retained live fixture.

## Reference agent cassettes

`langgraph_ref/` and `openai_agents_ref/` hold the cassettes the reference
outside agents (`trace_harness.agents.langgraph_ref:agent` and
`trace_harness.agents.openai_agents_ref:agent`) replay for
`refund_policy_valid_cash` and `refund_policy_failure`. The model behind them is
scripted. Each file was recorded by running the reference agent with the
task's fixture script as the model, so the responses are the script's actions
and the requests are what the agent's graph sent for them. No live model was
involved and none of these files is evidence of how a real model behaves.

Replay is strict in the same way as above. A run whose conversation drifts from
the recording, for example because an installed control blocked a call the
recording saw succeed, stops with a request mismatch at that step. Use
`:scripted_agent` for runs like that; it plays the fixture script directly.

Reproduce the recording into a new directory, from the repository root.

```sh
python scripts/record_reference_cassettes.py langgraph_ref --root /tmp/reference-cassettes
python scripts/record_reference_cassettes.py openai_agents_ref --root /tmp/reference-cassettes
```

The reference agent tests re-record both files and require them to match the
committed bytes. They also require the transcript fingerprint at every step to
match the one the harness runner builds for a fixture model on the same task.
The tool fingerprints match too for the Agents SDK agent, which passes the
schemas through untouched. They differ for LangGraph, because LangChain rewrites
the JSON schemas it binds (titles dropped, references inlined).
