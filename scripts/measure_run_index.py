#!/usr/bin/env python3
"""Measure the run index at sweep scale (issue #213, TRA-134).

The ticket gates any index work on two thresholds: ``list-runs`` over a sweep
directory taking more than five seconds, or ``index.json`` passing fifty
megabytes. This script takes that measurement.

For each scale it builds a synthetic runs directory by copying the retained run
directories under ``docs/acceptance`` and giving every copy a fresh run id. It
then replays the index writes a ``run-suite`` sweep makes, in the order the
runner makes them and through the real ``ArtifactStore`` methods::

    upsert_index_entry              # agent_runner, when the run finalizes
    enrich_index_entry_with_verifier  # pipeline, after verifier_result.json
    enrich_index_entry_with_batch   # BatchRunner, after the batch summary

Each of those reads the whole index and rewrites it, so the total write cost of
a sweep grows with the square of its size. Above ``--full-write-max`` runs the
full replay would take hours, so the total is estimated instead from the
per-run cost measured at evenly spaced index sizes. Every scale small enough
for a full replay also gets the estimate, which shows how close it lands.

The script stops with an error instead of timing a directory it cannot vouch
for. A replayed index must equal a fresh ``rebuild_index``, the sampled
estimate must leave the index exactly as the full sweep left it, and before any
listing is timed the index must hold exactly the run ids on disk. Without that
last check a stale index would make ``RunReader.list_runs`` rebuild on every
call, and the listing figures would silently time rebuilds.

Once the index is written, the script times listing four ways over the same
directory: ``trace-harness list-runs`` as a subprocess, ``RunReader.list_runs``
in process, ``ArtifactStore.rebuild_index`` (what listing pays once when the
index is stale), and the dashboard's server-side ``listRuns()`` loaded under
node. ``--next-dev`` also times a full render of ``/runs`` under ``next dev``,
run from a copy of ``apps/dashboard`` so its ``.next`` output never lands in
the repository.

``--probe N`` adds a listing-only point at N runs. Probe directories hold only
the three files the index path reads (``run_result.json``, ``run_config.json``,
``verifier_result.json``) and their index comes from one ``rebuild_index``, so
they locate where the thresholds trip without hours of copying and replay.

``--end-to-end SEEDS`` cross-checks the replay against the real thing. It runs
the refund_v0 suite through ``BatchRunner`` SEEDS times with two fixture agent
configs standing in for two providers, and times every index call the runner
makes along the way. Each invocation is one batch of both configs, 64 runs,
where the replay batches the 32 runs of one provider and seed.

Everything the script writes goes into a new directory it creates under
``--work``, so a folder already there is never reused or removed. That
directory is deleted at the end unless ``--keep`` is given.

Usage (from the repo root, with the package importable)::

    python scripts/measure_run_index.py --work /tmp/scale213 --json /tmp/scale213/results.json

The script lives outside ``tests/`` and has no ``test_`` prefix, so pytest never
collects it and CI never runs it. ``tests/test_measure_run_index.py`` covers its
checks and its handling of the work directory at the smallest scale.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import random
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trace_harness.run_reader import RunReader
from trace_harness.runner.config import RunConfig
from trace_harness.runner.result import RunResult
from trace_harness.tracing import artifact_store as names
from trace_harness.tracing.artifact_store import ArtifactStore
from trace_harness.tracing.run_index import RUN_INDEX_SCHEMA_VERSION, RunIndex, RunIndexEntry

REPO_ROOT = Path(__file__).resolve().parents[1]
RETAINED_ROOT = REPO_ROOT / "docs" / "acceptance"
SWEEP_SUITE = REPO_ROOT / "fixtures" / "suites" / "refund_v0.json"
DASHBOARD = REPO_ROOT / "apps" / "dashboard"

SWEEP_PROVIDERS = 2
SWEEP_SEEDS = 5
LIST_RUNS_THRESHOLD_S = 5.0
INDEX_SIZE_THRESHOLD_BYTES = 50_000_000  # "fifty megabytes", read in decimal units
# The files the index path reads. A probe directory holds only these.
INDEX_INPUTS = (names.RUN_RESULT, names.RUN_CONFIG, names.VERIFIER_RESULT)

NODE_HOOKS = """\
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const SRC = process.env.DASHBOARD_SRC;

export async function resolve(specifier, context, next) {
  if (specifier.startsWith("@/")) {
    const base = path.join(SRC, specifier.slice(2));
    for (const candidate of [`${base}.ts`, `${base}.tsx`, path.join(base, "index.ts")]) {
      if (existsSync(candidate)) return next(pathToFileURL(candidate).href, context);
    }
  }
  return next(specifier, context);
}
"""

NODE_BENCH = """\
import { register } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

register("./hooks.mjs", import.meta.url);
const loader = path.join(process.env.DASHBOARD_SRC, "data", "run-loader.ts");
const { listRuns } = await import(pathToFileURL(loader).href);
const reps = Number(process.env.REPS);
const ms = [];
let count = 0;
for (let i = 0; i < reps; i += 1) {
  const start = process.hrtime.bigint();
  count = listRuns().length;
  ms.push(Number(process.hrtime.bigint() - start) / 1e6);
}
console.log(JSON.stringify({ count, ms }));
"""


@dataclass(frozen=True)
class Scale:
    name: str
    runs: int
    description: str


@dataclass(frozen=True)
class PlannedRun:
    run_id: str
    result: RunResult
    config: RunConfig


@dataclass(frozen=True)
class Batch:
    batch_id: str
    runs: tuple[PlannedRun, ...]


# --- setup ---


def retained_templates() -> list[Path]:
    """Every retained run directory that has a result, in a stable order."""
    return sorted(p.parent for p in RETAINED_ROOT.rglob(names.RUN_RESULT))


def sweep_task_count() -> int:
    return len(json.loads(SWEEP_SUITE.read_text(encoding="utf-8"))["tasks"])


def build_scales(templates: list[Path], tasks: int) -> list[Scale]:
    sweep = SWEEP_PROVIDERS * SWEEP_SEEDS * tasks
    return [
        Scale("retained", len(templates), "every retained run under docs/acceptance"),
        Scale("sweep", sweep, f"{SWEEP_PROVIDERS} providers x {SWEEP_SEEDS} seeds x {tasks}"),
        Scale("sweep_two_domains", 2 * sweep, "one sweep plus a second domain of equal size"),
        Scale("sweep_x10", 10 * sweep, "ten sweeps"),
        Scale("sweep_x50", 50 * sweep, "fifty sweeps"),
    ]


def fresh_run_ids(count: int, seed: int) -> list[str]:
    """Sortable ids in the runner's format, one second apart, deterministic per seed."""
    rng = random.Random(seed)
    base = datetime(2026, 10, 1, tzinfo=UTC)
    return [
        f"run_{base + timedelta(seconds=i):%Y%m%dT%H%M%SZ}_{rng.getrandbits(32):08x}"
        for i in range(count)
    ]


def copy_run(template: Path, target: Path, run_id: str, only: tuple[str, ...] | None) -> None:
    """Copy one run directory, rewriting its run id wherever a file carries it."""
    target.mkdir(parents=True)
    old = template.name.encode()
    new = run_id.encode()
    for source in template.iterdir():
        if only is not None and source.name not in only:
            continue
        data = source.read_bytes()
        if old in data:
            (target / source.name).write_bytes(data.replace(old, new))
        else:
            shutil.copyfile(source, target / source.name)


def new_session_dir(work: Path) -> Path:
    """Create the directory that holds everything one invocation writes.

    Every scale, probe, the empty floor directory, the node harness and the
    dashboard copy live inside it. A folder that already sat in ``work`` is
    never reused, so the cleanup below can only remove what this run made.
    """
    work.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="measure_run_index_", dir=work))


def build_runs_dir(
    runs_dir: Path, count: int, templates: list[Path], seed: int, only: tuple[str, ...] | None
) -> list[str]:
    runs_dir.mkdir()  # raises FileExistsError rather than reuse a directory
    run_ids = fresh_run_ids(count, seed)
    for i, run_id in enumerate(run_ids):
        copy_run(templates[i % len(templates)], runs_dir / run_id, run_id, only)
    return run_ids


def plan_batches(store: ArtifactStore, run_ids: list[str], batch_size: int) -> list[Batch]:
    """Load each run's result and config, grouped the way run-suite batches them."""
    planned = [
        PlannedRun(
            run_id=run_id,
            result=RunResult.model_validate(store.read_json(run_id, names.RUN_RESULT)),
            config=RunConfig.model_validate(store.read_json(run_id, names.RUN_CONFIG)),
        )
        for run_id in run_ids
    ]
    return [
        Batch(batch_id=f"batch_synthetic_{i // batch_size:05d}", runs=tuple(chunk))
        for i in range(0, len(planned), batch_size)
        for chunk in [planned[i : i + batch_size]]
    ]


def batch_summary_payload(batch: Batch) -> dict[str, Any]:
    """The fields rebuild_index reads from a batch summary, nothing more."""
    return {
        "batch_id": batch.batch_id,
        "suite_id": "synthetic_sweep",
        "entries": [{"run_id": run.run_id} for run in batch.runs],
    }


# --- write path ---


def index_ops_for_run(store: ArtifactStore, run: PlannedRun) -> None:
    store.upsert_index_entry(RunIndexEntry.from_result(run.result, run.config))
    store.enrich_index_entry_with_verifier(run.run_id)


def replay_writes(store: ArtifactStore, batches: list[Batch]) -> dict[str, Any]:
    """Replay a sweep's index writes from an empty index; return per-run costs."""
    store.index_path().unlink(missing_ok=True)
    shutil.rmtree(store.runs_dir / names.BATCHES_DIR, ignore_errors=True)
    costs: list[float] = []
    for batch in batches:
        batch_costs = []
        for run in batch.runs:
            start = time.perf_counter()
            index_ops_for_run(store, run)
            batch_costs.append(time.perf_counter() - start)
        store.write_batch_summary(batch.batch_id, batch_summary_payload(batch))
        for i, run in enumerate(batch.runs):
            start = time.perf_counter()
            store.enrich_index_entry_with_batch(run.run_id, batch.batch_id)
            batch_costs[i] += time.perf_counter() - start
        costs.extend(batch_costs)
    last = batches[-1].runs
    return {
        "total_s": sum(costs),
        "last_batch_per_run_ms": statistics.mean(costs[-len(last) :]) * 1000,
    }


def sampled_write_estimate(
    store: ArtifactStore,
    batches: list[Batch],
    final_entries: list[RunIndexEntry],
    samples: int,
    reps: int,
) -> dict[str, Any]:
    """Estimate a full replay's total from per-run costs at evenly spaced index sizes.

    At each sampled position k the index is set to the first k finished entries
    (untimed), then run k's three index calls are timed. Repetitions sweep all
    positions before repeating any, so a burst of load from elsewhere on the
    machine lands on one pass rather than on every repetition of one point. The
    total is the trapezoid sum of the per-position medians over the sweep.

    The last position is always the sweep's final run, so the index this leaves
    behind is what the real calls made from the first ``count - 1`` entries. It
    is not rewritten afterwards, which lets the caller check that it equals the
    full sweep's index.
    """
    if samples < 2:
        raise ValueError("the estimate needs at least two sampled positions")
    runs = [(run, batch.batch_id) for batch in batches for run in batch.runs]
    count = len(runs)
    positions = sorted({round(i * (count - 1) / (samples - 1)) for i in range(samples)})
    timings: dict[int, list[float]] = {k: [] for k in positions}
    for _ in range(reps):
        for k in positions:
            run, batch_id = runs[k]
            # Untimed setup. The store's own writer, so the file the timed calls
            # read is byte for byte the file the runner would have written.
            store._write_index(RunIndex(entries=final_entries[:k]))
            start = time.perf_counter()
            index_ops_for_run(store, run)
            store.enrich_index_entry_with_batch(run.run_id, batch_id)
            timings[k].append(time.perf_counter() - start)
    points = [(k, statistics.median(timings[k])) for k in positions]
    # Sum of c(k) over k = 0..n-1: the trapezoid integral plus half of each end.
    total = (points[0][1] + points[-1][1]) / 2
    for (k0, c0), (k1, c1) in zip(points, points[1:], strict=False):
        total += (k1 - k0) * (c0 + c1) / 2
    return {
        "estimated_total_s": total,
        "per_run_ms_at": {str(k): round(c * 1000, 2) for k, c in points},
    }


# --- read path ---


def check_index_matches_dirs(runs_dir: Path, run_ids: list[str]) -> None:
    """Refuse to time listing unless the index holds exactly the runs on disk.

    ``RunReader.list_runs`` rebuilds the index whenever the ids in it differ
    from the listable run directories, so a mismatch would turn every listing
    sample into a rebuild. The expected ids are the ones this script created.
    The file is parsed directly because ``read_index`` would quietly rebuild an
    unreadable or outdated index and hide the problem.
    """
    store = ArtifactStore(runs_dir)
    expected = set(run_ids)
    listable = {r for r in store.list_runs() if store.exists(r, names.RUN_RESULT)}
    index = RunIndex.model_validate_json(store.index_path().read_text(encoding="utf-8"))
    indexed = [entry.run_id for entry in index.entries]
    problems = []
    if index.schema_version != RUN_INDEX_SCHEMA_VERSION:
        problems.append(
            f"index schema {index.schema_version} where the store writes {RUN_INDEX_SCHEMA_VERSION}"
        )
    if listable != expected:
        problems.append(
            f"{len(listable - expected)} unexpected and {len(expected - listable)} missing run dirs"
        )
    if set(indexed) != expected:
        problems.append(
            f"{len(set(indexed) - expected)} unexpected and "
            f"{len(expected - set(indexed))} missing index entries"
        )
    if len(indexed) != len(set(indexed)):
        problems.append(f"{len(indexed) - len(set(indexed))} duplicate index entries")
    if problems:
        raise RuntimeError(f"{runs_dir}: {'; '.join(problems)}; listing would time a rebuild")


def summarize(samples: list[float]) -> dict[str, float]:
    return {
        "median": statistics.median(samples),
        "max": max(samples),
        "min": min(samples),
        "n": len(samples),
    }


def timed(fn: Callable[[], int], reps: int) -> tuple[list[float], int]:
    """Time ``fn`` ``reps`` times, collecting garbage between calls.

    ``fn`` returns a count so no result outlives its call, and each repetition
    starts from a heap close to what a fresh CLI process has.
    """
    out: list[float] = []
    value = 0
    for _ in range(reps):
        gc.collect()
        start = time.perf_counter()
        value = fn()
        out.append(time.perf_counter() - start)
    return out, value


def cli_command() -> list[str]:
    script = Path(sys.executable).with_name("trace-harness")
    return [str(script)] if script.is_file() else [sys.executable, "-m", "trace_harness.cli"]


def child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env.update(extra)
    return env


def list_runs_command(runs_dir: Path) -> list[str]:
    return [*cli_command(), "--runs-dir", str(runs_dir), "list-runs"]


def time_command(command: list[str]) -> float:
    start = time.perf_counter()
    subprocess.run(command, stdout=subprocess.DEVNULL, env=child_env(), check=True)
    return time.perf_counter() - start


def time_cli_list(
    runs_dir: Path, reps: int, expected: int | None, floor_dir: Path | None = None
) -> dict[str, Any]:
    """Time ``trace-harness list-runs`` as a subprocess.

    With ``floor_dir`` each timed call is paired with one over an empty runs
    dir taken right beside it. Interpreter startup dominates small scales and
    swings with machine load, so the paired difference isolates what listing
    the directory itself costs.
    """
    command = list_runs_command(runs_dir)
    warm = subprocess.run(command, capture_output=True, text=True, env=child_env(), check=True)
    last = warm.stdout.strip().splitlines()[-1] if warm.stdout.strip() else ""
    if expected is not None and not last.startswith(f"{expected} run(s)"):
        raise RuntimeError(f"list-runs over {runs_dir} printed {last!r}, expected {expected}")
    samples: list[float] = []
    floors: list[float] = []
    for _ in range(reps):
        if floor_dir is not None:
            floors.append(time_command(list_runs_command(floor_dir)))
        samples.append(time_command(command))
    out: dict[str, Any] = summarize(samples)
    if floors:
        out["empty_dir"] = summarize(floors)
        out["over_empty_dir"] = summarize([s - f for s, f in zip(samples, floors, strict=True)])
    return out


def time_reader_list(runs_dir: Path, reps: int, expected: int) -> dict[str, Any]:
    samples, listed = timed(lambda: len(RunReader.from_runs_dir(runs_dir).list_runs()), reps)
    if listed != expected:
        raise RuntimeError(f"RunReader listed {listed} runs, expected {expected}")
    return summarize(samples)


def time_rebuild(runs_dir: Path, reps: int) -> dict[str, Any]:
    samples, _ = timed(lambda: len(ArtifactStore(runs_dir).rebuild_index().entries), reps)
    return summarize(samples)


def time_dashboard_list(runs_dir: Path, work: Path, reps: int) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        return {"error": "node not found"}
    harness = work / "node_harness"
    harness.mkdir(parents=True, exist_ok=True)
    (harness / "hooks.mjs").write_text(NODE_HOOKS, encoding="utf-8")
    (harness / "bench.mjs").write_text(NODE_BENCH, encoding="utf-8")
    env = child_env(
        TRACE_RUNS_DIR=str(runs_dir), DASHBOARD_SRC=str(DASHBOARD / "src"), REPS=str(reps + 1)
    )
    proc = subprocess.run(
        [node, "--no-warnings", "--experimental-transform-types", str(harness / "bench.mjs")],
        capture_output=True,
        text=True,
        env=env,
        cwd=harness,
        check=False,
    )
    if proc.returncode != 0:
        return {"error": proc.stderr.strip()[-400:]}
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    seconds = [ms / 1000 for ms in data["ms"]]
    # The first call pays module JIT warm-up; a running server pays it once.
    return {**summarize(seconds[1:]), "first_call": seconds[0], "count": data["count"]}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def dashboard_copy(work: Path) -> Path:
    """A copy of ``apps/dashboard`` under ``work`` for ``next dev`` to run from.

    ``next dev`` writes ``.next`` into its working directory, which in the
    repository would replace the output of a ``next build``. The copy leaves out
    ``node_modules``, ``.next`` and local env files, and links the installed
    ``node_modules`` in place of copying it.
    """
    copy = work / "dashboard"
    if not copy.exists():
        shutil.copytree(
            DASHBOARD, copy, ignore=shutil.ignore_patterns("node_modules", ".next", ".env*")
        )
        (copy / "node_modules").symlink_to(DASHBOARD / "node_modules", target_is_directory=True)
    return copy


def time_next_dev_render(runs_dir: Path, work: Path, reps: int) -> dict[str, Any]:
    """Time GET /runs under ``next dev``; the page is prerendered by ``next build``."""
    if not (DASHBOARD / "node_modules" / ".bin" / "next").is_file():
        return {"error": "dashboard node_modules not installed"}
    app = dashboard_copy(work)
    port = free_port()
    env = child_env(TRACE_RUNS_DIR=str(runs_dir), NEXT_TELEMETRY_DISABLED="1")
    proc = subprocess.Popen(
        [str(app / "node_modules" / ".bin" / "next"), "dev", "-p", str(port)],
        cwd=app,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    ready = threading.Event()

    def drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            if "Ready" in line:
                ready.set()

    threading.Thread(target=drain, daemon=True).start()
    url = f"http://127.0.0.1:{port}/runs"
    try:
        if not ready.wait(timeout=120):
            return {"error": "next dev did not report ready"}

        def fetch() -> bytes:
            with urllib.request.urlopen(url, timeout=900) as response:
                return response.read()

        compile_start = time.perf_counter()
        fetch()
        first = time.perf_counter() - compile_start
        samples, html_bytes = timed(lambda: len(fetch()), reps)
        return {**summarize(samples), "first_request": first, "html_bytes": html_bytes}
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)


def measure_listing(
    runs_dir: Path, run_ids: list[str], work: Path, args: argparse.Namespace
) -> dict[str, Any]:
    check_index_matches_dirs(runs_dir, run_ids)
    count = len(run_ids)
    out: dict[str, Any] = {
        "index_bytes": (runs_dir / names.RUN_INDEX).stat().st_size,
        "list_runs_cli_s": time_cli_list(
            runs_dir, args.list_reps, count, floor_dir=empty_runs_dir(work)
        ),
        "reader_list_runs_s": time_reader_list(runs_dir, args.list_reps, count),
    }
    if not args.no_dashboard:
        dashboard = time_dashboard_list(runs_dir, work, args.list_reps)
        if "count" in dashboard and dashboard["count"] != count:
            seen = dashboard["count"]
            raise RuntimeError(f"dashboard listRuns() saw {seen} runs, expected {count}")
        out["dashboard_list_runs_s"] = dashboard
    if args.next_dev and count <= args.next_dev_max:
        out["next_dev_runs_page_s"] = time_next_dev_render(runs_dir, work, args.next_dev_reps)
    return out


# --- scales ---


def measure_scale(
    scale: Scale, templates: list[Path], batch_size: int, work: Path, args: argparse.Namespace
) -> dict[str, Any]:
    runs_dir = work / scale.name
    print(f"[{scale.name}] building {scale.runs} runs in {runs_dir}", flush=True)
    run_ids = build_runs_dir(runs_dir, scale.runs, templates, seed=scale.runs, only=None)
    store = ArtifactStore(runs_dir)
    batches = plan_batches(store, run_ids, batch_size)
    out: dict[str, Any] = {
        "scale": scale.name,
        "description": scale.description,
        "runs": scale.runs,
        "batches": len(batches),
        "loadavg_start": os.getloadavg(),
    }

    if scale.runs <= args.full_write_max:
        replays = []
        for rep in range(args.write_reps):
            print(f"[{scale.name}] write replay {rep + 1}/{args.write_reps}", flush=True)
            replays.append(replay_writes(store, batches))
        out["write_total_s"] = summarize([r["total_s"] for r in replays])
        out["write_last_batch_per_run_ms"] = summarize(
            [r["last_batch_per_run_ms"] for r in replays]
        )
        replayed = store.read_index()
        if replayed != store.rebuild_index():
            raise RuntimeError(f"[{scale.name}] the replayed index differs from rebuild_index")
        out["rebuild_index_s"] = time_rebuild(runs_dir, args.write_reps)
    else:
        # Too large for a full replay. Write batch summaries, rebuild once, then sample.
        for batch in batches:
            store.write_batch_summary(batch.batch_id, batch_summary_payload(batch))
        out["rebuild_index_s"] = time_rebuild(runs_dir, args.write_reps)
    final_entries = store.read_index().entries
    print(f"[{scale.name}] sampled write estimate", flush=True)
    out["write_estimate"] = sampled_write_estimate(
        store, batches, final_entries, args.samples, args.write_reps
    )
    if store.read_index().entries != final_entries:
        raise RuntimeError(f"[{scale.name}] sampling left the index different from the full sweep")
    # Listing in process should see a heap like a fresh CLI's, without the plan.
    del batches, final_entries

    print(f"[{scale.name}] listing", flush=True)
    out.update(measure_listing(runs_dir, run_ids, work, args))
    if not args.keep:
        shutil.rmtree(runs_dir)
    return out


def measure_probe(
    count: int, templates: list[Path], batch_size: int, work: Path, args: argparse.Namespace
) -> dict[str, Any]:
    runs_dir = work / f"probe_{count}"
    print(f"[probe_{count}] building {count} index-input-only runs", flush=True)
    run_ids = build_runs_dir(runs_dir, count, templates, seed=count, only=INDEX_INPUTS)
    store = ArtifactStore(runs_dir)
    for start in range(0, count, batch_size):
        chunk = run_ids[start : start + batch_size]
        batch_id = f"batch_synthetic_{start // batch_size:05d}"
        store.write_batch_summary(
            batch_id,
            {"batch_id": batch_id, "entries": [{"run_id": r} for r in chunk]},
        )
    out: dict[str, Any] = {
        "scale": f"probe_{count}",
        "description": "listing-only probe; run dirs hold only the index inputs",
        "runs": count,
        "loadavg_start": os.getloadavg(),
        "rebuild_index_s": time_rebuild(runs_dir, 1),
    }
    print(f"[probe_{count}] listing", flush=True)
    out.update(measure_listing(runs_dir, run_ids, work, args))
    if not args.keep:
        shutil.rmtree(runs_dir)
    return out


def measure_end_to_end(seeds: int, work: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Run real fixture-provider sweeps through BatchRunner and time its index calls.

    This cross-checks the replay. Two fixture agent configs stand in for the two
    providers and each suite invocation stands in for one seed, so each batch
    holds both configs' runs. Only the outermost index call is timed, because
    the enrich calls upsert inside.
    """
    from trace_harness.runner.batch import BatchRunner
    from trace_harness.runner.suite import load_suite

    runs_dir = work / "end_to_end"
    runs_dir.mkdir()  # raises FileExistsError rather than reuse a directory
    store = ArtifactStore(runs_dir)
    suite = load_suite(SWEEP_SUITE)
    base = suite.agent_configs[0]
    suite = suite.model_copy(
        update={
            "tasks": [str(REPO_ROOT / task) for task in suite.tasks],
            "agent_configs": [
                base.model_copy(update={"label": f"{base.label}-{i}"})
                for i in range(SWEEP_PROVIDERS)
            ],
        }
    )
    per_run: dict[str, float] = {}
    depth = 0
    originals: dict[str, Any] = {}

    def wrap(name: str, run_id_of: Callable[..., str]) -> None:
        original = getattr(ArtifactStore, name)
        originals[name] = original

        def timed_call(self: ArtifactStore, *a: Any, **kw: Any) -> Any:
            nonlocal depth
            depth += 1
            start = time.perf_counter()
            try:
                return original(self, *a, **kw)
            finally:
                depth -= 1
                if depth == 0:
                    run_id = run_id_of(*a, **kw)
                    per_run[run_id] = per_run.get(run_id, 0.0) + time.perf_counter() - start

        setattr(ArtifactStore, name, timed_call)

    wrap("upsert_index_entry", lambda entry: entry.run_id)
    wrap("enrich_index_entry_with_verifier", lambda run_id: run_id)
    wrap("enrich_index_entry_with_batch", lambda run_id, batch_id: run_id)
    print(f"[end_to_end] {seeds} run-suite invocations of {len(suite.tasks)} tasks", flush=True)
    loadavg_start = os.getloadavg()
    run_ids: list[str] = []
    last_batch: list[str] = []
    start = time.perf_counter()
    try:
        for _ in range(seeds):
            summary = BatchRunner(store).run(suite)
            last_batch = [e.run_id for e in summary.entries if e.run_id is not None]
            run_ids.extend(last_batch)
    finally:
        wall = time.perf_counter() - start
        for name, original in originals.items():
            setattr(ArtifactStore, name, original)
    if set(per_run) != set(run_ids):
        raise RuntimeError("[end_to_end] the timed index calls do not cover the batch runs")
    if store.read_index() != store.rebuild_index():
        raise RuntimeError("[end_to_end] the runner's index differs from rebuild_index")
    count = len(run_ids)
    index_total = sum(per_run.values())
    out: dict[str, Any] = {
        "scale": "end_to_end",
        "description": "real fixture-provider sweep through BatchRunner, index calls timed",
        "runs": count,
        "loadavg_start": loadavg_start,
        "sweep_wall_s": wall,
        "index_share_of_wall": index_total / wall,
        "write_total_s": summarize([index_total]),
        "write_last_batch_per_run_ms": summarize(
            [statistics.mean(per_run[r] for r in last_batch) * 1000]
        ),
        "rebuild_index_s": time_rebuild(runs_dir, 1),
    }
    out.update(measure_listing(runs_dir, run_ids, work, args))
    if not args.keep:
        shutil.rmtree(runs_dir)
    return out


def empty_runs_dir(work: Path) -> Path:
    empty = work / "empty"
    empty.mkdir(parents=True, exist_ok=True)
    return empty


def cli_floor(work: Path, reps: int) -> dict[str, Any]:
    return time_cli_list(empty_runs_dir(work), reps, expected=None)


def machine() -> dict[str, Any]:
    def sysctl(key: str) -> str:
        try:
            return subprocess.run(
                ["sysctl", "-n", key], capture_output=True, text=True, check=True
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return "unknown"

    node = shutil.which("node")
    node_version = (
        subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
        if node
        else None
    )
    return {
        "uname": " ".join(platform.uname()),
        "cpu": sysctl("machdep.cpu.brand_string"),
        "ncpu": sysctl("hw.ncpu"),
        "memory_bytes": sysctl("hw.memsize"),
        "macos": platform.mac_ver()[0],
        "python": sys.version.split()[0],
        "node": node_version,
        "loadavg_start": os.getloadavg(),
    }


# --- report ---


def ms(value: float) -> str:
    if value < 0.01:
        return f"{value * 1000:.2f} ms"
    return f"{value * 1000:,.0f} ms" if value < 10 else f"{value:,.1f} s"


def pair(stats: dict[str, Any] | None) -> str:
    if not stats:
        return "not measured"
    if "error" in stats:
        return "error"
    return f"{ms(stats['median'])} / {ms(stats['max'])}"


def size(n: int) -> str:
    return f"{n / 1000:,.0f} kB" if n < 1_000_000 else f"{n / 1_000_000:,.2f} MB"


def render_table(results: list[dict[str, Any]]) -> str:
    render = any("next_dev_runs_page_s" in r for r in results)
    header = (
        "| scale | runs | index.json | index writes over the sweep | per-run index cost at end "
        "| list-runs CLI | CLI above empty dir | RunReader.list_runs | rebuild_index "
        "| dashboard listRuns |"
        + (" GET /runs on next dev |" if render else "")
        + "\n|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
        + ("---:|" if render else "")
    )
    rows = [header]
    for r in results:
        if "write_total_s" in r:
            writes = pair(r["write_total_s"])
            per_run = f"{r['write_last_batch_per_run_ms']['median']:,.1f} ms"
        elif "write_estimate" in r:
            writes = f"about {ms(r['write_estimate']['estimated_total_s'])} (estimated)"
            last = list(r["write_estimate"]["per_run_ms_at"].values())[-1]
            per_run = f"{last:,.1f} ms"
        else:
            writes = per_run = "not measured"
        rows.append(
            f"| {r['scale']} | {r['runs']:,} | {size(r['index_bytes'])} | {writes} | {per_run} "
            f"| {pair(r['list_runs_cli_s'])} | {pair(r['list_runs_cli_s'].get('over_empty_dir'))} "
            f"| {pair(r['reader_list_runs_s'])} "
            f"| {pair(r['rebuild_index_s'])} | {pair(r.get('dashboard_list_runs_s'))} |"
            + (f" {pair(r.get('next_dev_runs_page_s'))} |" if render else "")
        )
    return "\n".join(rows)


def run_measurements(
    scales: list[Scale], templates: list[Path], tasks: int, work: Path, args: argparse.Namespace
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "machine": machine(),
        "templates": [str(t.relative_to(REPO_ROOT)) for t in templates],
        "sweep_tasks": tasks,
        "cli_floor_s": cli_floor(work, args.list_reps),
        "results": [],
    }
    for scale in scales:
        report["results"].append(measure_scale(scale, templates, tasks, work, args))
        if args.json:
            args.json.write_text(json.dumps(report, indent=2, default=str) + "\n")
    for count in args.probe:
        report["results"].append(measure_probe(count, templates, tasks, work, args))
        if args.json:
            args.json.write_text(json.dumps(report, indent=2, default=str) + "\n")
    if args.end_to_end:
        report["results"].append(measure_end_to_end(args.end_to_end, work, args))
    report["machine"]["loadavg_end"] = os.getloadavg()
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", required=True, type=Path, help="scratch dir outside the repo")
    parser.add_argument(
        "--scales",
        default=None,
        help="comma-separated scale names (default all; an empty value runs none)",
    )
    parser.add_argument("--write-reps", type=int, default=3)
    parser.add_argument("--list-reps", type=int, default=5)
    parser.add_argument("--full-write-max", type=int, default=3200)
    parser.add_argument("--samples", type=int, default=17)
    parser.add_argument("--probe", type=int, action="append", default=[])
    parser.add_argument(
        "--end-to-end",
        type=int,
        default=0,
        metavar="SEEDS",
        help="also run SEEDS real fixture run-suite invocations and time their index calls",
    )
    parser.add_argument("--no-dashboard", action="store_true")
    parser.add_argument("--next-dev", action="store_true", help="also time GET /runs on next dev")
    parser.add_argument("--next-dev-max", type=int, default=3200, help="skip the render above this")
    parser.add_argument("--next-dev-reps", type=int, default=3)
    parser.add_argument(
        "--keep", action="store_true", help="keep the directory this run creates under --work"
    )
    parser.add_argument("--json", type=Path, default=None, help="write raw results here")
    args = parser.parse_args(argv)

    work = args.work.resolve()
    if work == REPO_ROOT or REPO_ROOT in work.parents:
        parser.error("--work must be outside the repository")
    if args.samples < 2:
        parser.error("--samples must be at least 2")

    templates = retained_templates()
    tasks = sweep_task_count()
    scales = build_scales(templates, tasks)
    if args.scales is not None:
        wanted = {name for name in args.scales.split(",") if name}
        unknown = wanted - {s.name for s in scales}
        if unknown:
            parser.error(f"unknown scales: {', '.join(sorted(unknown))}")
        scales = [s for s in scales if s.name in wanted]

    session = new_session_dir(work)
    print(f"writing under {session}", flush=True)
    try:
        report = run_measurements(scales, templates, tasks, session, args)
    finally:
        if args.keep:
            print(f"kept {session}", flush=True)
        else:
            shutil.rmtree(session)

    print()
    print(render_table(report["results"]))
    print(f"\nlist-runs CLI over an empty dir: {pair(report['cli_floor_s'])} (median / max)")
    for r in report["results"]:
        tripped = []
        if r["index_bytes"] > INDEX_SIZE_THRESHOLD_BYTES:
            tripped.append("index size")
        if r["list_runs_cli_s"]["median"] > LIST_RUNS_THRESHOLD_S:
            tripped.append("list-runs time")
        print(f"{r['scale']}: {', '.join(tripped) if tripped else 'no threshold tripped'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
