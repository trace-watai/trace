# ADR-0003: Versioned control library with retained evidence and rollback

**Status:** Proposed for #147 review.

## Decision

Store accepted `ControlInstance`s in `fixtures/controls/library.json` at
schema `0.1.0`. Each entry contains its control, provenance, current status,
and append-only status history. Evidence paths are relative to the library
directory and carry SHA-256 hashes, so the library can move with its evidence.
The loader checks identities, required run snapshots, hashes, and registered
implementations. Rolled-back entries retain their provenance requirements.

Promotion requires explicit `replay --apply-control --commit`. It verifies
accepted individual evidence and replays the proposed active set against
both new and previously accepted regressions, including their siblings.
Only a successful gate publishes the new manifest. Source runs, individual
validation runs, activation runs, and the activation-check summary are retained.

Use explicit library loading on environments, single runs, and suites.
Controls apply in sorted ID order, and a batch freezes its set before running.
Run configuration metadata records the installed control instances. Default
fixture behavior remains the baseline used by existing expectations.

Rollback appends a reason and changes status to `rolled_back`. Entries and
evidence are never removed by rollback; IDs cannot be reused. Writers take an
exclusive lock and replace the manifest atomically after evidence is written.
A failed write leaves the previous manifest intact. A lock left by an
interrupted process needs removal after confirming no writer is running.

## Evidence and limits

The retained refund control passes the control demo and valid-cash sibling.
With the library active, all 18 previously passing `refund_v0` cases still pass.
Two negative cases replace unauthorized refunds with false refund claims;
the suite remains 18 passing and 14 failing. Controlled expectations live
beside the library. Existing baseline pins remain applicable without it.
Rollback restores the baseline verdicts and final states exactly.

This is an explicit local promotion mechanism, subject to TPM review before
landing. It does not establish live-agent recovery. ADR-0002's replay-validity
limits still apply; siblings currently use live fixture inputs, and the full
regression CI collector remains #161. Conflict policy beyond sorted IDs and
reactivation of rolled-back IDs are outside this version.
