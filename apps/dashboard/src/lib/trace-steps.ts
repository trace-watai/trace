/**
 * Groups a run's flat trace into one record per agent decision step, and
 * attaches attribution's causal markers and the verifier's failed checks to
 * the step they actually happened on.
 */

import type { AttributionResult } from "@/types/attribution";
import type { TraceEvent, TraceEventType } from "@/types/trace-event";
import type { FailedCheck, VerifierResult } from "@/types/verifier-result";

export type RetrievalResultItem =
  TraceEvent<"retrieval_result">["payload"]["results"][number];

export type StepMarkerKind =
  | "root_cause"
  | "first_bad"
  | "missed_recovery"
  | "first_unrecoverable"
  | "first_irreversible_action"
  | "visible_symptom";

export interface StepMarker {
  kind: StepMarkerKind;
}

export type StepAction =
  | { kind: "tool_call"; toolName: string; arguments: Record<string, unknown> }
  | { kind: "final_answer"; finalAnswer: string }
  | { kind: "none" };

export interface StepObservation {
  status: string;
  result: unknown;
  error: string | null;
}

export interface TraceStep {
  stepId: number;
  reasoning: string | null;
  action: StepAction;
  observation: StepObservation | null;
  retrievalResults: RetrievalResultItem[];
  markers: StepMarker[];
  failedChecks: FailedCheck[];
  events: TraceEvent[];
}

const findEvent = <T extends TraceEventType>(
  events: TraceEvent[],
  eventType: T,
): TraceEvent<T> | undefined =>
  events.find((event): event is TraceEvent<T> => event.eventType === eventType);

const findEvents = <T extends TraceEventType>(
  events: TraceEvent[],
  eventType: T,
): TraceEvent<T>[] =>
  events.filter(
    (event): event is TraceEvent<T> => event.eventType === eventType,
  );

const markersForStep = (
  stepId: number,
  attribution: AttributionResult | null,
): StepMarker[] => {
  if (!attribution) return [];
  const kinds: StepMarkerKind[] = [];
  if (attribution.rootCauseStep === stepId) kinds.push("root_cause");
  if (attribution.firstBadStep === stepId) kinds.push("first_bad");
  if (attribution.missedRecoveryStep === stepId) kinds.push("missed_recovery");
  if (attribution.firstUnrecoverableStep === stepId) {
    kinds.push("first_unrecoverable");
  }
  if (attribution.firstIrreversibleActionStep === stepId) {
    kinds.push("first_irreversible_action");
  }
  if (attribution.visibleSymptomSteps.includes(stepId)) {
    kinds.push("visible_symptom");
  }
  return kinds.map((kind) => ({ kind }));
};

const buildStep = (
  stepId: number,
  events: TraceEvent[],
  attribution: AttributionResult | null,
  verifier: VerifierResult | null,
): TraceStep => {
  const modelAction = findEvent(events, "model_action");
  const toolCallRequested = findEvent(events, "tool_call_requested");
  const observationEvent = findEvent(events, "tool_observation");
  const finalAnswerEvent = findEvent(events, "final_answer");
  const retrievalResults = findEvents(events, "retrieval_result").flatMap(
    (event) => event.payload.results,
  );

  const action: StepAction = finalAnswerEvent
    ? {
        kind: "final_answer",
        finalAnswer: finalAnswerEvent.payload.finalAnswer,
      }
    : toolCallRequested
      ? {
          kind: "tool_call",
          toolName: toolCallRequested.payload.toolName,
          arguments: toolCallRequested.payload.arguments,
        }
      : { kind: "none" };

  return {
    stepId,
    reasoning: modelAction?.payload.reasoning ?? null,
    action,
    observation: observationEvent
      ? {
          status: observationEvent.payload.status,
          result: observationEvent.payload.result,
          error: observationEvent.payload.error ?? null,
        }
      : null,
    retrievalResults,
    markers: markersForStep(stepId, attribution),
    failedChecks:
      verifier?.failedChecks.filter((check) =>
        check.stepIds.includes(stepId),
      ) ?? [],
    events,
  };
};

/** One record per agent decision step, ordered by step id. Run-level events
 * (step id `null`: run_started, state_snapshot, run_finished) are excluded. */
export const buildTraceSteps = (
  trace: TraceEvent[],
  attribution: AttributionResult | null,
  verifier: VerifierResult | null,
): TraceStep[] => {
  const byStep = new Map<number, TraceEvent[]>();
  for (const event of trace) {
    if (event.stepId === null) continue;
    const events = byStep.get(event.stepId);
    if (events) {
      events.push(event);
    } else {
      byStep.set(event.stepId, [event]);
    }
  }

  return [...byStep.entries()]
    .sort(([a], [b]) => a - b)
    .map(([stepId, events]) =>
      buildStep(stepId, events, attribution, verifier),
    );
};

/** One-line summary of what a step did, for the card title. */
export const stepTitle = (step: TraceStep): string => {
  switch (step.action.kind) {
    case "tool_call":
      return `Called \`${step.action.toolName}\``;
    case "final_answer":
      return "Final answer";
    default:
      return `Step ${step.stepId}`;
  }
};

const MARKER_LABELS: Record<StepMarkerKind, string> = {
  root_cause: "Root cause",
  first_bad: "First bad step",
  missed_recovery: "Missed recovery",
  first_unrecoverable: "First unrecoverable",
  first_irreversible_action: "First irreversible action",
  visible_symptom: "Visible symptom",
};

export const stepMarkerLabel = (kind: StepMarkerKind): string =>
  MARKER_LABELS[kind];
