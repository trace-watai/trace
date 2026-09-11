import Link from "next/link";
import { notFound } from "next/navigation";

import { ErrorPanel } from "@/components/run/error-panel";
import { TraceStepper } from "@/components/trace/trace-stepper";
import {
  getAttribution,
  getTrace,
  getVerifier,
  MalformedArtifactError,
  RunNotFoundError,
} from "@/data/run-loader";
import { buildTraceSteps, type TraceStep } from "@/lib/trace-steps";

interface TracePageProps {
  params: Promise<{ runId: string }>;
  searchParams: Promise<{ step?: string }>;
}

/** The requested step id if it names a real step, else the first step. */
const resolveCurrentStepId = (
  requestedStep: string | undefined,
  steps: TraceStep[],
): number => {
  const requested = requestedStep ? Number(requestedStep) : NaN;
  if (
    Number.isFinite(requested) &&
    steps.some((step) => step.stepId === requested)
  ) {
    return requested;
  }
  return steps[0]?.stepId ?? 1;
};

type TraceDataResult =
  | { status: "ok"; steps: TraceStep[] }
  | { status: "malformed"; message: string };

const loadTraceData = (runId: string): TraceDataResult => {
  try {
    const trace = getTrace(runId);
    const attribution = getAttribution(runId);
    const verifier = getVerifier(runId);
    return {
      status: "ok",
      steps: buildTraceSteps(trace, attribution, verifier),
    };
  } catch (error) {
    if (error instanceof RunNotFoundError) {
      return notFound();
    }
    if (error instanceof MalformedArtifactError) {
      return { status: "malformed", message: error.message };
    }
    throw error;
  }
};

const TracePage = async ({ params, searchParams }: TracePageProps) => {
  const { runId } = await params;
  const { step } = await searchParams;
  const result = loadTraceData(runId);

  return (
    <main className="mx-auto w-[90vw] max-w-6xl py-12">
      <header className="mb-8 flex items-start justify-between gap-4">
        <div className="space-y-2">
          <h1 className="text-2xl font-semibold tracking-tight">
            <span className="text-primary">TRACE</span> Trace
          </h1>
          <p className="text-sm text-muted-foreground">
            Step-by-step trace for <span className="font-mono">{runId}</span>
          </p>
        </div>
        <Link
          href={`/runs/${runId}`}
          className="shrink-0 text-sm font-medium text-primary hover:underline"
        >
          Failure card →
        </Link>
      </header>

      {result.status === "malformed" ? (
        <ErrorPanel title="Malformed run data" message={result.message} />
      ) : (
        <TraceStepper
          steps={result.steps}
          runId={runId}
          currentStepId={resolveCurrentStepId(step, result.steps)}
        />
      )}
    </main>
  );
};

export default TracePage;
