"""Retain a sweep's failing cells as evidence that replays offline (#198).

A live model is not deterministic, so a failing cell is kept exactly as it ran.
:func:`retain_failing_cells` copies each failing cell's run directory and its
cassette into ``docs/acceptance/runs/live-sweep-<date>-<suffix>/``, where the
suffix is the last part of the sweep id, next to the sweep summary, a run index,
and a README with one triage row per cell. The regression gate already collects
every ``regression_artifact.json`` under ``docs/acceptance/runs``, so retained
failures gate once they are committed.

Nothing lands unless all of it passes. The copy is assembled in a temporary
folder under the sweep's own directory in ``runs/``, where no gate looks, and
it becomes the target only after two checks. Every cell is replayed from its
copied cassette, offline, and has to give the verdict and checks it gave live.
Every file is then scanned with :mod:`trace_harness.secret_scan`, the way the
live Gemini runs retained for #179 were scanned, for the values of the three
provider key variables as set when retention runs, for every key shape that
module knows, and for the local home, working and runs directories. A finding
names the file, the line and the kind and never the value.

The one change to a copied run is in ``run_config.json``. Its cassette
directory and cassette path are rewritten to where the recording sits in the
retained folder, relative to that folder, because the sweep recorded them as
paths on the machine that ran it.
"""

from __future__ import annotations

import errno
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from trace_harness.models.cassette import CassetteConfig
from trace_harness.runner.config import RunConfig
from trace_harness.runner.pipeline import run_task_pipeline
from trace_harness.runner.suite import AgentConfig
from trace_harness.runner.sweep import SWEEP_SUMMARY, sweep_dir
from trace_harness.runner.sweep_summary import SWEEP_CASSETTES, SweepFailingCell, SweepSummary
from trace_harness.secret_scan import PROVIDER_KEY_VARIABLES, SHAPES, key_values, scan_paths
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.verifiers.base import VerifierResult

RETAIN_ROOT = Path("docs/acceptance/runs")
RETAINED_PREFIX = "live-sweep-"
#: Where the checked copy is assembled, under the sweep's own directory.
STAGING_PREFIX = "retaining-"


class RetentionError(ValueError):
    """Retention refused, and nothing was written to the target."""


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


def local_paths(store: ArtifactStore) -> list[tuple[str, str]]:
    """This machine's home, working and runs directories, to search for by value."""
    paths = [Path.cwd(), store.runs_dir.resolve()]
    try:
        paths.append(Path.home())
    except RuntimeError:
        pass
    return [("local path", str(path)) for path in paths]


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


def retained_folder_name(summary: SweepSummary) -> str:
    """``live-sweep-<date>-<suffix>``, dated by the sweep's start.

    The suffix is the random part of the sweep id, so two sweeps started on
    one day retain into two folders, and one sweep always names one folder.
    """
    suffix = summary.sweep_id.rsplit("_", 1)[-1]
    return f"{RETAINED_PREFIX}{summary.started_at:%Y-%m-%d}-{suffix}"


def retain_failing_cells(
    store: ArtifactStore, sweep_id: str, retain_root: Path = RETAIN_ROOT
) -> Path | None:
    """Retain the sweep's failing cells; None when nothing failed."""
    source = sweep_dir(store.runs_dir, sweep_id)
    summary = SweepSummary.model_validate_json((source / SWEEP_SUMMARY).read_text())
    if not summary.failing_cells:
        return None
    target = retain_root / retained_folder_name(summary)
    if target.exists():
        raise RetentionError(f"{target} already exists; move it aside or choose another root")
    work = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=source))
    try:
        for cell in summary.failing_cells:
            shutil.copytree(store.run_dir(cell.run_id), work / cell.run_id)
            _point_at_retained_cassette(work / cell.run_id / names.RUN_CONFIG, cell)
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

        hits = scan_paths([work], values=key_values() + local_paths(store), relative_to=work)
        if hits:
            listed = "; ".join(str(hit) for hit in hits)
            raise RetentionError(f"secret scan found {len(hits)} hit(s): {listed}")
        retain_root.mkdir(parents=True, exist_ok=True)
        _move_into_place(work, target)
        return target
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _point_at_retained_cassette(path: Path, cell: SweepFailingCell) -> None:
    """Rewrite a copied run config's cassette paths relative to the retained folder."""
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("cassette") is not None:
        config["cassette"]["directory"] = SWEEP_CASSETTES
    if "cassette_path" in config.get("metadata", {}):
        config["metadata"]["cassette_path"] = cell.cassette_path
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _move_into_place(work: Path, target: Path) -> None:
    """Rename the checked copy to the target, which then appears whole."""
    try:
        work.rename(target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # The runs directory is on another filesystem. Copy under a hidden
        # name beside the target first, so the target still appears whole.
        hidden = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
        try:
            shutil.copytree(work, hidden, dirs_exist_ok=True)
            hidden.rename(target)
        finally:
            shutil.rmtree(hidden, ignore_errors=True)


def render_triage_readme(summary: SweepSummary) -> str:
    """The retained folder's README, with one row per cell for a person to triage."""
    budget = summary.budget
    cap = f" of a ${budget.max_cost_usd:.2f} cap" if budget else ""
    keys = _listed(PROVIDER_KEY_VARIABLES)
    shapes = _listed([kind for kind, _ in SHAPES])
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
        "The one change is that each `run_config.json` names its cassette relative to this "
        "folder. `sweep_summary.json` is the summary the sweep wrote, and `docs/live_sweep.md` "
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
        "No key, auth header or local path appears in any file here. Before retention "
        f"every file was scanned for the values of {keys}, for the home, working and runs "
        f"directories of the machine that retained it, and for these shapes, {shapes}, "
        "each matched as written and again with JSON and URL escapes decoded. There were "
        "no hits.",
        "",
    ]
    return "\n".join(lines)


def _listed(items: list[str] | tuple[str, ...]) -> str:
    return f"{', '.join(items[:-1])} and {items[-1]}"
