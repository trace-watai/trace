import { FailureCard } from "@/components/failure/failure-card";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { loadRefundFailureFixture } from "@/data/refund-failure-fixture";

const HomePage = () => {
  const fixture = loadRefundFailureFixture();
  const { runResult, verifierResult } = fixture;

  return (
    <main className="container max-w-4xl py-12">
      <header className="mb-8 space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">
          <span className="text-primary">TRACE</span> Dashboard
        </h1>
        <p className="text-sm text-muted-foreground">
          Generated failure artifacts, loaded as one coherent run.
        </p>
      </header>

      <Card className="mb-6 border-border/70 bg-card/80">
        <CardHeader>
          <CardTitle className="text-base">Run summary</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid gap-x-6 gap-y-5 sm:grid-cols-2 lg:grid-cols-3">
            <SummaryField label="Run ID" value={runResult.runId} mono />
            <SummaryField label="Task" value={runResult.taskId} mono />
            <SummaryField
              label="Run status"
              value={`${runResult.status} · ${runResult.terminationReason.replaceAll("_", " ")}`}
            />
            <SummaryField label="Steps" value={String(runResult.stepsTaken)} />
            <SummaryField
              label="Verifier"
              value={verifierResult.passed ? "PASS" : "FAIL"}
            />
            <SummaryField
              label="Severity"
              value={verifierResult.severity ?? "none"}
            />
          </dl>
          <p className="mt-5 border-t pt-4 text-xs text-muted-foreground">
            {fixture.artifactNames.length} canonical artifacts loaded
          </p>
        </CardContent>
      </Card>

      <FailureCard card={fixture.failureCard} />
    </main>
  );
};

interface SummaryFieldProps {
  label: string;
  value: string;
  mono?: boolean;
}

const SummaryField = ({ label, value, mono = false }: SummaryFieldProps) => (
  <div className="min-w-0 space-y-1">
    <dt className="text-[0.7rem] font-bold uppercase tracking-[0.14em] text-muted-foreground">
      {label}
    </dt>
    <dd
      className={
        mono
          ? "truncate font-mono text-sm text-foreground"
          : "text-sm font-medium capitalize text-foreground"
      }
      title={value}
    >
      {value}
    </dd>
  </div>
);

export default HomePage;
