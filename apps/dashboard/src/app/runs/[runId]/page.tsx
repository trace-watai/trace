import { notFound } from "next/navigation";

import { FailureCard } from "@/components/failure/failure-card";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
      <header className="mb-8 space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">
          <span className="text-primary">TRACE</span> Dashboard
        </h1>
        <p className="text-sm text-muted-foreground">Most recent runs</p>
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

const ErrorPanel = ({ title, message }: { title: string; message: string }) => (
  <Card className="border-destructive/40 bg-destructive/5">
    <CardHeader>
      <CardTitle className="text-base text-destructive">{title}</CardTitle>
    </CardHeader>
    <CardContent className="text-sm text-muted-foreground">
      {message}
    </CardContent>
  </Card>
);

export default RunPage;
