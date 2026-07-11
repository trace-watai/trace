# Refund-failure dashboard fixture

This bundle is the complete output of the deterministic
`fixtures/tasks/refund_policy_failure.json` pipeline run. It gives the dashboard
a realistic failed run for offline development and contract testing.

Regenerate all files from the repository root:

```bash
python scripts/generate_sample_outputs.py
```

The generator fixes the run ID and timestamps so rerunning it only changes this
bundle when the pipeline or an artifact schema changes. The JSON files preserve
the trace harness's canonical artifact formatting and are intentionally excluded
from dashboard Prettier.

Unlike the production `runs/{run_id}/` layout, this fixture is intentionally
flat. Its `run_result.json` rewrites `artifact_paths` to filenames that resolve
relative to this directory; all other artifact content matches pipeline output.
