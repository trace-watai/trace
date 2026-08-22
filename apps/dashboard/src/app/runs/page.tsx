import Link from "next/link";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { listRuns } from "@/data/run-loader";

const RunsPage = () => {
  const runs = [...listRuns()].sort(
    (a, b) => new Date(b.startedAt).getTime() - new Date(a.startedAt).getTime(),
  );

  return (
    <main className="container max-w-4xl py-12">
      <header className="mb-8 space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">
          <span className="text-primary">TRACE</span> Runs
        </h1>
        <p className="text-sm text-muted-foreground">
          Most recent runs
        </p>
      </header>

      {runs.length === 0 ? (
        <Card className="border-border/70 bg-card/80">
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            No runs found yet.
          </CardContent>
        </Card>
      ) : (
        <ul className="space-y-3">
          {runs.map((run) => (
            <li key={run.runId}>
              <Link href={`/runs/${run.runId}`}>
                <Card className="border-border/70 bg-card/80 transition-colors hover:border-primary/40">
                  <CardHeader className="gap-2">
                    <CardTitle className="text-base font-mono">
                      {run.runId}
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-muted-foreground">
                    <span>Task: {run.taskId}</span>
                    <span>
                      Status: {run.status} - {run.terminationReason.replaceAll("_", " ")}
                    </span>
                    <span>
                      Verifier:{" "}
                      {run.verifierPassed === null
                        ? "not yet verified"
                        : run.verifierPassed
                          ? "PASS"
                          : `FAIL (${run.failedCheckCount} checks)`}
                    </span>
                  </CardContent>
                </Card>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
};

export default RunsPage;
