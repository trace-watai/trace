"""The Python side of the public results SQL contract.

The tables are defined in ``supabase/migrations/``. This module names them for
the reader and the uploader, and a lockstep test checks every name here against
the newest migration, so a column renamed on one side fails the gate.

``RESULTS_SCHEMA_VERSION`` versions the SQL layout the same way every JSON
contract in the harness carries a ``schema_version``. It changes only when a
migration changes the tables. A bump to an artifact schema upstream (FailureCard,
RunIndexEntry, Experiment and the rest) changes what sits inside the jsonb
columns and needs no migration.
"""

from __future__ import annotations

# 0.1.0: runs, batches and experiments with artifacts as jsonb (#205).
RESULTS_SCHEMA_VERSION = "0.1.0"

MIGRATIONS_DIR = "supabase/migrations"

SCHEMA_VERSIONS = "schema_versions"
RUNS = "runs"
BATCHES = "batches"
EXPERIMENTS = "experiments"

# The tables the uploader writes and prunes, in write order, with the natural
# key each one is upserted on.
PRIMARY_KEYS: dict[str, str] = {
    RUNS: "run_id",
    BATCHES: "batch_id",
    EXPERIMENTS: "experiment_id",
}

CONTENT_SHA256 = "content_sha256"

COLUMNS: dict[str, tuple[str, ...]] = {
    SCHEMA_VERSIONS: ("version", "description", "applied_at"),
    RUNS: (
        "run_id",
        "task_id",
        "batch_id",
        "summary",
        "run_result",
        "task_spec",
        "trace",
        "verifier_result",
        "attribution_result",
        "failure_card",
        "repair_package",
        "regression_artifact",
        "bundle_ref",
        CONTENT_SHA256,
    ),
    BATCHES: ("batch_id", "summary", "suite_report", CONTENT_SHA256),
    EXPERIMENTS: ("experiment_id", "spec", "result", CONTENT_SHA256),
}

# Fields left out of the content hash because RunReader does not return them
# the same way twice. A suite report that was never persisted is built in
# memory on every read and stamped with the current time, so hashing that stamp
# would make every upload look like a change.
VOLATILE_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    BATCHES: (("suite_report", "generated_at"),),
}
