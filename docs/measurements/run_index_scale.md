# Run index at sweep scale

Measured on 2026-09-23 for issue #213 (TRA-134) with
[`scripts/measure_run_index.py`](../../scripts/measure_run_index.py).

The ticket builds a sqlite index beside `index.json` only if
`trace-harness list-runs` over a sweep directory takes more than five seconds
or `index.json` passes fifty megabytes. At one sweep plus a second domain (640
runs) `list-runs` took 0.24 s and the index was 343 kB. Neither threshold
trips, so no index code was written. Both lines are crossed only near 100,000
runs in one directory, about 300 sweeps.

## Scales

A sweep is 2 providers by 5 seeds by the 32 tasks in
`fixtures/suites/refund_v0.json`, which makes 320 runs.

| Scale | Runs | Stands for |
|---|---:|---|
| retained | 15 | every retained run under `docs/acceptance` |
| sweep | 320 | one sweep |
| sweep_two_domains | 640 | one sweep plus a second domain of the same size |
| sweep_x10 | 3,200 | ten sweeps in one directory |
| sweep_x50 | 16,000 | fifty sweeps in one directory |
| probe_50000 | 50,000 | listing-only point |
| probe_100000 | 100,000 | listing-only point |
| end_to_end | 320 | one real fixture sweep through `BatchRunner` |

## Method

Each synthetic directory copies the 15 retained runs in turn and rewrites the
run id in every file that carries it, so each copy lists, verifies and
rebuilds as its own run. The script then replays the index writes of a sweep
through the real `ArtifactStore` methods, in the order the runner makes them.
`upsert_index_entry` runs when a run finalizes, `enrich_index_entry_with_verifier`
after the verify stage, and `enrich_index_entry_with_batch` for each run once
its batch of 32 has a summary. Every replayed index equaled a fresh
`rebuild_index` entry for entry.

A full replay at 50x would take over an hour per repetition. That total is
estimated instead from the per-run index cost timed at 17 evenly spaced index
sizes and summed over the sweep. At 10x the estimate came within 7 percent of
the full replay (128 s against 138 s).

The end-to-end row runs the refund_v0 suite through `BatchRunner` five times,
with two fixture agent configs standing in for two providers, and times every
index call the runner makes. It confirms the replay exercises the path a real
sweep takes.

The probes hold only the three files the index path reads (`run_result.json`,
`run_config.json`, `verifier_result.json`) and get their index from one
`rebuild_index`. Listing reads nothing else, so the probes time it faithfully
without copying and replaying 100,000 full runs.

Listing is timed five ways over the same directory.

- `trace-harness list-runs` as a subprocess. Each call is paired with one over
  an empty runs directory taken right beside it, and the "above empty dir"
  column is the median of the paired differences. Interpreter startup alone
  cost 0.17 to 0.8 s on this machine depending on load.
- `RunReader.list_runs` in process.
- `ArtifactStore.rebuild_index`, which `list_runs` pays once whenever the
  index is missing a run that is on disk.
- The dashboard's `listRuns()` from `apps/dashboard/src/data/run-loader.ts`,
  imported under node with type stripping and a resolve hook for `@/`. The
  first call is left out as warm-up.
- `GET /runs` under `next dev`, up to 3,200 runs. `next build` prerenders
  `/runs` as a static page, so under `next start` a request would time a
  static file written at build time.

Listing figures are the median and max of five repetitions. Write replays and
rebuilds are three repetitions, except the probe rebuilds, which ran once.

## Machine

Apple M1 Pro, 8 cores, 16 GB, macOS 15.6.1 (Darwin 24.6.0, arm64), APFS on the
internal SSD, Python 3.11.13, node 22.23.2.

The machine was shared with other agents running test suites and builds for
the whole measurement. The one-minute load average was between 18 and 69 when
each scale started, and swap held 12.0 of 13.3 GB by the end. The timings are
pessimistic for an idle machine and noisy at the small scales, which is why
the CLI column is paired against an empty directory.

## Results

Final pass, median / max.

### Size and writes

| Scale | Runs | Load at start | index.json | Index writes over the sweep | Per-run index cost at the end |
|---|---:|---:|---:|---:|---:|
| retained | 15 | 69 | 8 kB | 60 ms / 77 ms | 4.0 ms |
| sweep | 320 | 54 | 172 kB | 9.4 s / 12.5 s | 33 ms |
| sweep_two_domains | 640 | 38 | 343 kB | 7.7 s / 8.0 s | 23 ms |
| sweep_x10 | 3,200 | 23 | 1.72 MB | 138 s / 138 s | 82 ms |
| sweep_x50 | 16,000 | 18 | 8.58 MB | about 79 min, estimated | 631 ms |
| probe_50000 | 50,000 | 25 | 26.8 MB | not measured | not measured |
| probe_100000 | 100,000 | 27 | 53.6 MB | not measured | not measured |
| end_to_end | 320 | 34 | 184 kB | 1.9 s (one sweep) | 11 ms |

### Listing

| Scale | list-runs CLI | CLI above empty dir | RunReader.list_runs | rebuild_index | Dashboard listRuns() | GET /runs on next dev |
|---|---:|---:|---:|---:|---:|---:|
| retained | 480 ms / 941 ms | 0 ms / 352 ms | 0.9 ms / 1.1 ms | 9 ms / 11 ms | 0.09 ms / 0.26 ms | 0.41 s / 0.73 s |
| sweep | 1.00 s / 1.45 s | 269 ms / 296 ms | 15 ms / 18 ms | 122 ms / 147 ms | 1.4 ms / 2.4 ms | 1.6 s / 2.4 s |
| sweep_two_domains | 240 ms / 270 ms | 29 ms / 35 ms | 12 ms / 13 ms | 103 ms / 107 ms | 0.9 ms / 1.2 ms | 3.7 s / 3.7 s |
| sweep_x10 | 223 ms / 231 ms | 55 ms / 60 ms | 53 ms / 56 ms | 395 ms / 439 ms | 3.3 ms / 3.6 ms | 30.8 s / 36.6 s |
| sweep_x50 | 544 ms / 847 ms | 344 ms / 661 ms | 337 ms / 349 ms | 4.7 s / 5.2 s | 23 ms / 30 ms | not measured |
| probe_50000 | 1.24 s / 1.25 s | 1.07 s / 1.09 s | 0.97 s / 1.08 s | 16.8 s | 70 ms / 85 ms | not measured |
| probe_100000 | 2.82 s / 4.07 s | 2.59 s / 3.29 s | 2.80 s / 2.90 s | 77.3 s | 193 ms / 247 ms | not measured |
| end_to_end | 174 ms / 184 ms | 4 ms / 11 ms | 5.3 ms / 5.8 ms | 50 ms | 0.4 ms / 0.9 ms | 1.07 s / 1.27 s |

### Earlier pass

An earlier pass ran the same scales the same evening, plus a probe at 200,000
runs. It used a revision of the script whose in-process timing kept the
previous repetition's result alive, so only the figures that revision did not
affect are repeated here. The spread between the two passes comes from the
machine's load.

| Runs | Load at start | index.json | list-runs CLI | Index writes over the sweep |
|---:|---:|---:|---:|---:|
| 15 | 22 | 8 kB | 0.58 s / 0.73 s | 80 ms |
| 320 | 22 | 172 kB | 0.54 s / 0.75 s | 3.9 s |
| 640 | 35 | 343 kB | 0.50 s / 0.90 s | 19.2 s |
| 3,200 | 27 | 1.72 MB | 0.31 s / 0.37 s | 184 s |
| 16,000 | 12 | 8.58 MB | 0.65 s / 0.66 s | about 93 min, estimated |
| 100,000 | 31 | 53.6 MB | 6.49 s / 11.1 s | not measured |
| 200,000 | 55 | 107 MB | 25.6 s / 37.7 s | not measured |

## Decision

The thresholds are 5 s for `list-runs` and 50 MB for `index.json`. At one
sweep plus a second domain `list-runs` took 0.24 s (0.50 s in the earlier
pass), a twentieth of the time line, and the index was 343 kB, under a
hundredth of the size line. The index stays JSON and no sqlite index was
built.

## Where each threshold trips

- Index size is linear, 536 bytes per entry for the copied runs and 574 for
  the real fixture sweep. It passes 50 MB between 87,000 and 93,000 runs,
  which is 270 to 290 sweeps in one directory. The 100,000-run probe measured
  53.6 MB.
- `list-runs` costs about 21 microseconds per run above interpreter startup up
  to 50,000 runs and 26 at 100,000. At 100,000 runs that time splits between
  parsing the index, the directory scan with two `stat` calls per run, and
  building the summaries, and the scan is the largest part. It stayed under
  5 s at 100,000 runs in the final pass (2.8 s), crossed it there in the
  earlier pass under heavier load (6.5 s), and took 25.6 s at 200,000. On this
  machine it crosses 5 s between 100,000 and 200,000 runs, and sooner when the
  machine is swapping.
- A stale index crosses 5 s much sooner. `RunReader.list_runs` rebuilds from
  every run's artifacts when the index misses a run on disk, and that rebuild
  took 4.7 s (5.2 s max) at 16,000 runs. It happens once per mismatch, and
  listing is fast again afterwards. At 640 runs the rebuild is 0.1 s.

## Costs outside the two thresholds

- Total index writes grow with the square of the run count. Each run reads the
  whole index five times and rewrites it three times, and every rewrite is fsynced.
  The per-run cost at the end of a sweep was 23 ms at 640 runs, 82 ms at 3,200
  and 631 ms at 16,000. A sweep's writes summed to 8 to 19 s at 640 runs and
  138 to 184 s at 3,200. In the real fixture sweep, index calls took 48 percent
  of the 3.9 s wall time because fixture runs finish in milliseconds. Live
  provider runs spend seconds per model call, so the share stays small until a
  directory holds thousands of runs.
- The dashboard run list slows from rendering, and the index read barely
  registers. `listRuns()` stays under 25 ms at 16,000 runs and under 250 ms at
  100,000. `GET /runs` under `next dev` renders every run on one page and took
  3.7 s at 640 runs and 31 s at 3,200, returning 15 MB of HTML. In a production
  build `/runs` is prerendered, so it shows the runs present at the last
  `next build`. Paging the list would bound the render with either index
  format.

## Reproduce

```
export PYTHONPATH=src
python scripts/measure_run_index.py --work /tmp/scale213/final --next-dev \
  --end-to-end 5 --probe 50000 --probe 100000 --json /tmp/scale213/final.json
```

The work directory must sit outside the repository, and each scale's
directory is removed once it is measured unless `--keep` is given. The final
pass took about 20 minutes on this machine, most of it in the 10x write
replays and the probe rebuilds.
