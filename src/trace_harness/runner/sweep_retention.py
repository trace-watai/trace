"""Retain a sweep's failing cells as evidence that replays offline (#198).

A live model is not deterministic, so a failing cell is kept exactly as it ran.
:func:`retain_failing_cells` copies each failing cell's run directory and its
cassette into ``docs/acceptance/runs/live-sweep-<date>/``, next to the sweep
summary, a run index, and a README with one triage row per cell. The regression
gate already collects every ``regression_artifact.json`` under
``docs/acceptance/runs``, so retained failures gate once they are committed.

Nothing lands unless all of it passes. The copy is assembled in a temporary
folder beside the target, and it becomes the target only after two checks.
Every cell is replayed from its copied cassette, offline, and has to give the
verdict and checks it gave live. Every file is then scanned for secrets, the
way the live Gemini runs retained for #179 were scanned. The scan looks for the
values of the three provider key variables as set when retention runs, for the
shapes of Google, Anthropic and OpenAI keys and of bearer tokens, and for auth
header fields. A finding names the file and the kind and never the value.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from trace_harness.models.cassette import CassetteConfig
from trace_harness.runner.config import RunConfig
from trace_harness.runner.pipeline import run_task_pipeline
from trace_harness.runner.suite import AgentConfig
from trace_harness.runner.sweep import SWEEP_SUMMARY, sweep_dir
from trace_harness.runner.sweep_summary import SWEEP_CASSETTES, SweepSummary
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.verifiers.base import VerifierResult

RETAIN_ROOT = Path("docs/acceptance/runs")
RETAINED_PREFIX = "live-sweep-"
KEY_VARIABLES = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
_SECRET_SHAPES = {
    "Google API key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "Google AQ. key": re.compile(r"\bAQ\.[0-9A-Za-z_\-]{20,}"),
    "Anthropic or OpenAI key": re.compile(r"\bsk-[0-9A-Za-z_\-]{20,}"),
    "bearer token": re.compile(r"(?i)\bbearer\s+[0-9A-Za-z._~+/\-]{16,}"),
    "auth header field": re.compile(
        r'(?i)"(?:authorization|proxy[-_]authorization|x[-_]api[-_]key|'
        r'x[-_]goog[-_]api[-_]key|api[-_]?key)"\s*:'
    ),
}


class RetentionError(ValueError):
    """Retention refused, and nothing was written to the target."""


@dataclass(frozen=True)
class SecretFinding:
    path: str
    kind: str


@dataclass(frozen=True)
class CellReplay:
    run_id: str
    verdict: str | None
    failed_check_ids: list[str]
    replayed_verdict: str | None
    replayed_check_ids: list[str]

    @property
    def matches(self) -> bool:
        return (self.verdict, self.failed_check_ids) == (
            self.replayed_verdict,
            self.replayed_check_ids,
        )


def key_values() -> list[tuple[str, str]]:
    """The provider keys set in the environment, to search for by value."""
    values = [(name, os.environ.get(name, "")) for name in KEY_VARIABLES]
    return [(name, value) for name, value in values if len(value) >= 8]


def scan_for_secrets(root: Path, keys: list[tuple[str, str]]) -> list[SecretFinding]:
    findings = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(root).as_posix()
        findings += [SecretFinding(relative, f"value of {n}") for n, v in keys if v in text]
        findings += [
            SecretFinding(relative, kind)
            for kind, pattern in _SECRET_SHAPES.items()
            if pattern.search(text)
        ]
    return findings


def replay_retained(folder: Path, run_id: str, store: ArtifactStore) -> CellReplay:
    """Run a retained cell again from the cassette beside it, calling nothing."""
    run_dir = folder / run_id
    config = RunConfig.model_validate_json((run_dir / names.RUN_CONFIG).read_text())
    original = VerifierResult.model_validate_json((run_dir / names.VERIFIER_RESULT).read_text())
    agent = AgentConfig(
        label=str(config.metadata.get("agent_label", config.provider)),
        provider=config.provider,
        model=config.model,
        prompt_version=config.prompt_version,
        temperature=config.temperature,
        seed=config.seed,
        max_steps=config.max_steps,
        timeout_seconds=config.timeout_seconds,
        cassette=CassetteConfig(mode="replay", directory=str(folder / SWEEP_CASSETTES)),
    )
    result = run_task_pipeline(
        config.metadata["task_fixture_path"], agent, store, bundle_on_fail=False
    )
    replayed = result.verifier_result
    return CellReplay(
        run_id=run_id,
        verdict=original.verdict.value if original.verdict else None,
        failed_check_ids=sorted(c.check_id for c in original.failed_checks),
        replayed_verdict=replayed.verdict.value if replayed and replayed.verdict else None,
        replayed_check_ids=sorted(c.check_id for c in replayed.failed_checks) if replayed else [],
    )


def retained_cells(root: Path) -> list[tuple[Path, str]]:
    """Every retained sweep cell under ``root``, as (folder, run id)."""
    return [
        (config.parent.parent, config.parent.name)
        for config in sorted(root.glob(f"{RETAINED_PREFIX}*/run_*/{names.RUN_CONFIG}"))
    ]


def retain_failing_cells(
    store: ArtifactStore, sweep_id: str, retain_root: Path = RETAIN_ROOT
) -> Path | None:
    """Retain the sweep's failing cells; None when nothing failed."""
    source = sweep_dir(store.runs_dir, sweep_id)
    summary = SweepSummary.model_validate_json((source / SWEEP_SUMMARY).read_text())
    if not summary.failing_cells:
        return None
    target = retain_root / f"{RETAINED_PREFIX}{summary.started_at:%Y-%m-%d}"
    if target.exists():
        raise RetentionError(f"{target} already exists; move it aside or choose another root")
    retain_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=retain_root))
    try:
        for cell in summary.failing_cells:
            shutil.copytree(store.run_dir(cell.run_id), work / cell.run_id)
            (work / cell.cassette_path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / cell.cassette_path, work / cell.cassette_path)
        (work / SWEEP_SUMMARY).write_text(summary.model_dump_json(indent=2) + "\n")

        replays = ArtifactStore(source / "retention-replays")
        for cell in summary.failing_cells:
            replay = replay_retained(work, cell.run_id, replays)
            if not replay.matches:
                raise RetentionError(
                    f"{cell.run_id} does not replay from its cassette: live "
                    f"{replay.verdict} {replay.failed_check_ids}, replayed "
                    f"{replay.replayed_verdict} {replay.replayed_check_ids}"
                )
        (work / "README.md").write_text(render_triage_readme(summary))
        ArtifactStore(work).rebuild_index()

        findings = scan_for_secrets(work, key_values())
        if findings:
            listed = "; ".join(f"{f.path} ({f.kind})" for f in findings)
            raise RetentionError(f"secret scan found {len(findings)} hit(s): {listed}")
        work.rename(target)
        return target
    finally:
        shutil.rmtree(work, ignore_errors=True)


def render_triage_readme(summary: SweepSummary) -> str:
    """The retained folder's README, with one row per cell for a person to triage."""
    budget = summary.budget
    cap = f" of a ${budget.max_cost_usd:.2f} cap" if budget else ""
    keys = f"{', '.join(KEY_VARIABLES[:-1])} and {KEY_VARIABLES[-1]}"
    stopped = (
        f" It stopped early as `{budget.stop_reason}`." if budget and budget.stop_reason else ""
    )
    models = " and ".join(f"`{p.model}`" for p in summary.providers)
    lines = [
        f"# Live sweep, {summary.started_at.day} {summary.started_at:%B %Y}",
        "",
        f"This folder keeps the failing cells of sweep `{summary.sweep_id}` "
        f"(`{summary.sweep_name}`), which ran {summary.runs} cells over {summary.task_count} "
        f"tasks under {models} for seeds {', '.join(map(str, summary.seeds))}. It spent "
        f"${summary.cost_usd:.4f}{cap}.{stopped} Of its cells {len(summary.failing_cells)} "
        f"failed, {summary.verified_failures} of them verified failures and "
        f"{summary.natural_verified_failures} of those natural.",
        "",
        "Each run directory is kept as it ran, and its model calls are under `cassettes/`. "
        "`sweep_summary.json` is the summary the sweep wrote, and `docs/live_sweep.md` "
        "defines the labels. The note column is for the person triaging each cell, one "
        "line on what the model did.",
        "",
        "| Run | Model | Seed | Task | Checks fired | Label | Why this label | Note |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| `{c.run_id}` | `{c.model}` | {c.seed} | `{c.task_id}` | "
        f"{', '.join(f'`{check}`' for check in c.failed_check_ids)} | {c.label} | "
        f"{c.label_reason} | Pending triage. |"
        for c in summary.failing_cells
    ]
    lines += [
        "",
        "Every cell was replayed from its cassette before it was retained and gave the "
        "verdict and checks it gave live. `tests/test_sweep_retention.py` replays every "
        "cell under `docs/acceptance/runs/live-sweep-*` again on each run of the suite, "
        "and the regression gate replays each failure from its regression artifact.",
        "",
        "```sh",
        "pytest tests/test_sweep_retention.py -k retained",
        "trace-harness collect-regressions docs/acceptance/runs",
        "```",
        "",
        "No key or auth header appears in any file here. Before retention every file was "
        f"scanned for the values of {keys}, for the shapes of Google, "
        "Anthropic and OpenAI keys and of bearer tokens, and for auth header fields, with "
        "no hits.",
        "",
    ]
    return "\n".join(lines)
