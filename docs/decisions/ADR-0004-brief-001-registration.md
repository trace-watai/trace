# ADR-0004: Brief 001 is registered as an existence test

**Status:** Proposed (2026-09-22). Becomes Accepted on merge.

## Context

ADR-0002 (decision 1) made the control replay-validity audit Phase 3's first
research brief, and issue #158 asked for the brief and its pre-registration.
Counting the sample before registering it showed that the suites hold far
fewer independent units than their task counts suggest. Of 18 distinct
failing tasks in `refund_v0` and `refund_bundles_v0`, 3 record a block under
the one registered control, and those 3 come from 2 task families. All 3 carry
the `live_required` label, so none of them can test whether `static_ok`
predicts agreement.

## Decision

1. Phase 3's first brief is
   [brief 001](../experiments/briefs/001-control-replay-validity.md),
   registered by [pre-registration 001](../experiments/preregistration/001.md).
   The pre-registration merges before any live arm runs and before anyone
   reads a static verdict, and its registration commit is recorded at merge.
2. Brief 001 is an existence test. It can show that a static replay verdict
   disagrees with live continuation after a block, and it reports no
   disagreement or over-blocking rate for controls in general. Certifying a
   rate under 5% at 95% confidence needs 59 clean independent units, and the
   registered sample has 2.
3. New controls registry entries enlarge the sample only through a dated
   amendment to the pre-registration made before the first live run.

## Consequences

A null result stays reportable with its bound, which is what ADR-0002 asked
of the pre-registration. Static replay verdicts on controls stay advisory
under ADR-0002 (decision 2) until a measured label replaces the one assigned
at materialization.

The decision has three costs. The brief cannot say how often static replay
misleads, which needs more independent families with blocks, from a second
domain or from families authored for the purpose. H3 goes untested unless an
amendment adds a registry entry that yields `static_ok` artifacts, and such an
entry changes the controls being measured. Every live arm waits on the branch
stage (#159), and H2 also waits on the post-block outcome classifier (#157).

This decision is revisited on a null result for brief 001, which ADR-0002
already names as a trigger, and when a second workflow environment lands.
