import { ErrorPanel } from "@/components/run/error-panel";
import { VerifierView } from "@/components/verifier/verifier-view";
import { getVerifier, MalformedArtifactError } from "@/data/run-loader";
import type { VerifierResult } from "@/types/verifier-result";

interface VerifierPageProps {
  params: Promise<{ runId: string }>;
  searchParams: Promise<{ check?: string }>;
}

type VerifierResultState =
  | { status: "ok"; result: VerifierResult | null }
  | { status: "malformed"; message: string };

const loadVerifier = (runId: string): VerifierResultState => {
  try {
    return { status: "ok", result: getVerifier(runId) };
  } catch (error) {
    if (error instanceof MalformedArtifactError) {
      return { status: "malformed", message: error.message };
    }
    throw error;
  }
};

const VerifierPage = async ({ params, searchParams }: VerifierPageProps) => {
  const { runId } = await params;
  const { check } = await searchParams;
  const state = loadVerifier(runId);

  return (
    <main className="container max-w-4xl pb-12 pt-8">
      {state.status === "malformed" ? (
        <ErrorPanel title="Malformed run data" message={state.message} />
      ) : state.result ? (
        <VerifierView
          runId={runId}
          result={state.result}
          currentCheckId={check}
        />
      ) : (
        <ErrorPanel
          title="Not yet verified"
          message="This run hasn't produced a verifier result yet — run the verifier stage on it first."
        />
      )}
    </main>
  );
};

export default VerifierPage;
