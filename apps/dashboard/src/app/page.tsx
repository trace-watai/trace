// Landing page. Renders one real failure card while the run view
// (/runs/[runId]) is still to come. See docs/future_dashboard.md.
import { FailureCard } from "@/components/failure/failure-card";
import { sampleFailureCard } from "@/data/sample-failure-card";

const HomePage = () => (
  <main className="container max-w-3xl py-12">
    <div className="mb-8 space-y-1">
      <h1 className="text-2xl font-semibold tracking-tight">
        <span className="text-primary">TRACE</span> Dashboard
      </h1>
      <p className="font-mono text-sm text-muted-foreground">
        Failure card: sample run {sampleFailureCard.runId}
      </p>
    </div>
    <FailureCard card={sampleFailureCard} />
  </main>
);

export default HomePage;
