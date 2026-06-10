"""Materialize the canonical sample run under traces/sample_runs/refund_run_001/.

Writes the file split that TRA-8 storage will produce, so the dashboard (TRA-27) and
other streams have a real fixture to build against today.

Usage:
    cd backend && python -m app.schemas.generate_sample_run
"""

from __future__ import annotations

import json
from pathlib import Path

from .examples import RUN_ID, build_sample_refund_run

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_json(path: Path, model) -> None:
    path.write_text(json.dumps(model.model_dump(mode="json"), indent=2) + "\n")


def generate(out_root: Path | None = None) -> Path:
    run = build_sample_refund_run()
    run.validate_ordering()
    out = (out_root or (_REPO_ROOT / "traces" / "sample_runs")) / RUN_ID
    out.mkdir(parents=True, exist_ok=True)

    _write_json(out / "run_metadata.json", run.metadata)
    _write_json(out / "agent_config.json", run.agent_config)
    _write_json(out / "task_snapshot.json", run.task_snapshot)
    if run.initial_state:
        _write_json(out / "initial_state.json", run.initial_state)
    if run.final_state:
        _write_json(out / "final_state.json", run.final_state)
    if run.verifier_result:
        _write_json(out / "verifier_result.json", run.verifier_result)
    if run.attribution:
        _write_json(out / "attribution.json", run.attribution)
    (out / "trace.jsonl").write_text(run.trace_jsonl() + "\n")
    return out


def main() -> int:
    out = generate()
    print(f"Wrote sample run to {out}")
    for p in sorted(out.iterdir()):
        print(f"  - {p.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
