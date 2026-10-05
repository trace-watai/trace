import { ErrorPanel } from "@/components/run/error-panel";
import { TraceStepper } from "@/components/trace/trace-stepper";
import {
  getAttribution,
  getTrace,
  getVerifier,
  MalformedArtifactError,
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
    <main className="mx-auto w-[90vw] max-w-6xl pb-12 pt-8">
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
