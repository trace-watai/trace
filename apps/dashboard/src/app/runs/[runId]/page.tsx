import { FailureCard } from "@/components/failure/failure-card";
import { ErrorPanel } from "@/components/run/error-panel";
import {
  getBundle,
  MalformedArtifactError,
  type FailureBundle,
} from "@/data/run-loader";

interface RunPageProps {
  params: Promise<{ runId: string }>;
}

type BundleResult =
  | { status: "ok"; bundle: FailureBundle | null }
  | { status: "malformed"; message: string };

const loadBundle = (runId: string): BundleResult => {
  try {
    return { status: "ok", bundle: getBundle(runId) };
  } catch (error) {
    if (error instanceof MalformedArtifactError) {
      return { status: "malformed", message: error.message };
    }
    throw error;
  }
};

const RunPage = async ({ params }: RunPageProps) => {
  const { runId } = await params;
  const result = loadBundle(runId);

  return (
    <main className="container max-w-4xl pb-12 pt-8">
      {result.status === "malformed" ? (
        <ErrorPanel title="Malformed run data" message={result.message} />
      ) : result.bundle ? (
        <FailureCard card={result.bundle.failureCard} />
      ) : (
        <ErrorPanel
          title="Not yet bundled"
          message="This run hasn't produced a failure card yet — run `trace-harness bundle` on it first."
        />
      )}
    </main>
  );
};

export default RunPage;
