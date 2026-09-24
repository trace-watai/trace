import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { loadMetricsHistory } from "@/data/metrics-history";
import { MetricSeries } from "@/components/metric-series";
import { describeOverBlocking } from "@/lib/over-blocking";
import { ratioValue, type MetricsSnapshot } from "@/types/metrics-snapshot";

export const dynamic = "force-dynamic";

const percent = (value: number | null): string =>
  value === null ? "not measured" : `${(value * 100).toFixed(1)}%`;

const SERIES = [
  {
    key: "coverage",
    title: "Control coverage",
    blurb:
      "Accepted controls over the controls repair packages asked for. ADR-0002 keeps a replay verdict advisory until its artifact carries a measured replay-mode label; like the collector, an acceptance on a static_ok artifact whose basis supports it counts as gating, and every static_ok label is predicted until #159 measures one. A5 in docs/methodology_metrics.md.",
    point: (snapshot: MetricsSnapshot) => ({
      value: ratioValue(snapshot.coverage.acceptedOverPrescribed),
      label: `${snapshot.coverage.accepted}/${snapshot.coverage.prescribed} accepted, ${snapshot.coverage.acceptedGating} gating on predicted labels and ${snapshot.coverage.acceptedAdvisory} advisory`,
      detail: `${snapshot.coverage.materializable} materializable, ${snapshot.coverage.validated} validated`,
    }),
    format: percent,
  },
  {
    key: "over-blocking",
    title: "Over-blocking",
    blurb:
      "Positive siblings that failed while a control was installed, from the latest validation. Siblings in one task family share a template, so the upper bound counts families. A4 and A6.",
    point: (snapshot: MetricsSnapshot) => ({
      value: ratioValue(snapshot.overBlocking.rate),
      label: describeOverBlocking(snapshot.overBlocking),
      detail: `${snapshot.overBlocking.siblingsFailed}/${snapshot.overBlocking.siblingsRun} siblings failed; ${snapshot.overBlocking.sources.join(", ")}`,
    }),
    format: percent,
  },
  {
    key: "cost-of-learning",
    title: "Cost of learning",
    blurb:
      "Money the validation re-runs moved, alongside the irreversible actions they took. A7.",
    point: (snapshot: MetricsSnapshot) => ({
      value: snapshot.costOfLearning.moneyMovedUsd,
      label: `$${snapshot.costOfLearning.moneyMovedUsd.toFixed(2)} moved`,
      detail: `${snapshot.costOfLearning.irreversibleActions} irreversible actions over ${snapshot.costOfLearning.validationRuns} runs`,
    }),
    format: (value: number | null) =>
      value === null ? "not measured" : `$${value.toFixed(2)}`,
  },
  {
    key: "suite-pass-rate",
    title: "Suite pass rate",
    blurb: "Verified passes over completed runs, for context under the rest.",
    point: (snapshot: MetricsSnapshot) => ({
      value: ratioValue(snapshot.suitePassRate),
      label: `${snapshot.suitePassRate.numerator}/${snapshot.suitePassRate.denominator} passed`,
      detail: `${snapshot.verifiedFailures} verified failures`,
    }),
    format: percent,
  },
] as const;

const MetricsPage = () => {
  const history = loadMetricsHistory();

  return (
    <main className="container max-w-4xl py-12">
      <header className="mb-8 space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">
          <span className="text-primary">TRACE</span> Metrics
        </h1>
        <p className="text-sm text-muted-foreground">
          One point per merge to main, read from the committed history file.
          Each series stands on its own, there is no combined score.
        </p>
      </header>

      {history.length === 0 ? (
        <Card className="border-border/70 bg-card/80">
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            No snapshots recorded yet.
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-6">
          {SERIES.map((series) => (
            <Card key={series.key} className="border-border/70 bg-card/80">
              <CardHeader className="gap-2">
                <CardTitle className="text-base">{series.title}</CardTitle>
                <p className="text-sm text-muted-foreground">{series.blurb}</p>
              </CardHeader>
              <CardContent>
                <MetricSeries
                  points={history.map((snapshot) => ({
                    commit: snapshot.commit,
                    recordedAt: snapshot.recordedAt,
                    ...series.point(snapshot),
                  }))}
                  format={series.format}
                />
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </main>
  );
};

export default MetricsPage;
