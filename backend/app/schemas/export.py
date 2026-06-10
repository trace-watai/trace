"""Export JSON Schema for every top-level TRACE model.

Usage:
    python -m app.schemas.export [out_dir]   # default: ../schemas_json relative to repo

Produces one ``<name>.schema.json`` per model plus ``trace_event.schema.json`` for
the trajectory event union. TRA-22 deliverable: JSON Schema export.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import TypeAdapter

from .common import TRACE_SCHEMA_VERSION
from .events import TraceEvent
from .run import AgentConfig, RunMetadata, StateSnapshot, TaskSnapshot
from .trace import TraceRun
from .verification import AttributionResult, VerifierResult

_MODELS = {
    "run_metadata": RunMetadata,
    "agent_config": AgentConfig,
    "task_snapshot": TaskSnapshot,
    "state_snapshot": StateSnapshot,
    "verifier_result": VerifierResult,
    "attribution_result": AttributionResult,
    "trace_run": TraceRun,
}


def build_schemas() -> dict[str, dict]:
    """Return {name: json_schema} for all exported models, including the event union."""
    schemas = {name: model.model_json_schema() for name, model in _MODELS.items()}
    schemas["trace_event"] = TypeAdapter(TraceEvent).json_schema()
    return schemas


def export_json_schemas(out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, schema in build_schemas().items():
        path = out / f"{name}.schema.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out_dir = argv[0] if argv else str(Path(__file__).resolve().parents[3] / "schemas_json")
    written = export_json_schemas(out_dir)
    print(f"TRACE schema v{TRACE_SCHEMA_VERSION}: wrote {len(written)} schema(s) to {out_dir}")
    for p in written:
        print(f"  - {p.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
