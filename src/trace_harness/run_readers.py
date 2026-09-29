"""Pick the RunReader backend from the environment.

``TRACE_RUN_READER`` selects where reads come from.

    unset, empty or "filesystem"  RunReader over the runs directory (the default)
    "supabase"                    SupabaseRunReader over the hosted public results,
                                  configured by TRACE_SUPABASE_URL and
                                  TRACE_SUPABASE_ANON_KEY

Both backends have the same read methods, so a caller that only reads, such as
``list-runs``, does not care which one it got. Commands that write artifacts
keep using the filesystem store directly. An unknown value is an error, so a
typo cannot fall back to the filesystem without a word.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from trace_harness.run_reader import RunReader
from trace_harness.run_reader_supabase import SupabaseRunReader
from trace_harness.tracing.artifact_store import ArtifactStore

BACKEND_ENV = "TRACE_RUN_READER"
FILESYSTEM = "filesystem"
SUPABASE = "supabase"
BACKENDS = (FILESYSTEM, SUPABASE)


def open_run_reader(
    store: ArtifactStore, env: Mapping[str, str] | None = None
) -> RunReader | SupabaseRunReader:
    """The reader ``TRACE_RUN_READER`` names, the filesystem one when it is unset."""
    env = os.environ if env is None else env
    backend = (env.get(BACKEND_ENV) or FILESYSTEM).strip().lower()
    if backend == FILESYSTEM:
        return RunReader(store)
    if backend == SUPABASE:
        return SupabaseRunReader.from_env(env)
    raise ValueError(f"{BACKEND_ENV} must be one of {', '.join(BACKENDS)} (got {backend!r})")


def reader_location(reader: RunReader | SupabaseRunReader) -> str:
    """Where a reader reads from, for messages."""
    if isinstance(reader, SupabaseRunReader):
        return reader.location
    return str(reader.store.runs_dir)
