import Link from "next/link";
import { notFound } from "next/navigation";

import { FailureCard } from "@/components/failure/failure-card";
import { ErrorPanel } from "@/components/run/error-panel";
import {
  getBundle,
  MalformedArtifactError,
  RunNotFoundError,
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
    if (error instanceof RunNotFoundError) {
      return notFound();
    }
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
    <main className="container max-w-4xl py-12">
      <header className="mb-8 flex items-start justify-between gap-4">
        <div className="space-y-2">
          <h1 className="text-2xl font-semibold tracking-tight">
            <span className="text-primary">TRACE</span> Dashboard
          </h1>
          <p className="text-sm text-muted-foreground">Most recent runs</p>
        </div>
        <Link
          href={`/runs/${runId}/trace`}
          className="shrink-0 text-sm font-medium text-primary hover:underline"
        >
          View trace →
        </Link>
      </header>

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
