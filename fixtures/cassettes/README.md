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
the provider. Use a new directory for a different recording: existing files are
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
