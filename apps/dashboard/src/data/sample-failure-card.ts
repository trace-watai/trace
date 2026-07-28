/**
 * Sample failure card for local development.
 *
 * The JSON is a real `failure_card.json` (schema 0.4.0) produced by
 * `trace-harness run-pipeline fixtures/tasks/refund_policy_failure.json`, per
 * docs/future_dashboard.md: render static JSON from real runs, not invented
 * dashboard state. Parsed through the same `parseFailureCard` path the
 * eventual loader will use.
 */

import { parseFailureCard, type RawFailureCard } from "@/types/failure-card";

import rawSampleFailureCard from "./sample-failure-card.json";

// The imported JSON widens enum-valued fields (e.g. evidence `kind`) to
// `string`, so cast through `unknown` to the wire type before parsing.
export const sampleFailureCard = parseFailureCard(
  rawSampleFailureCard as unknown as RawFailureCard,
);
