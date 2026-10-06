import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { RunTabs } from "@/components/run/run-tabs";
import { runExists } from "@/data/run-store";

interface RunLayoutProps {
  children: ReactNode;
  params: Promise<{ runId: string }>;
}

/**
 * Shared chrome for every `/runs/[runId]` view: title, tab row, and the one
 * run-exists check, so individual pages only handle their own artifact's
 * missing/malformed states. Pages own their `<main>` and width.
 */
const RunLayout = async ({ children, params }: RunLayoutProps) => {
  const { runId } = await params;
  if (!runExists(runId)) notFound();

  return (
    <>
      <div className="container max-w-4xl space-y-6 pt-12">
        <header className="space-y-2">
          <h1 className="text-2xl font-semibold tracking-tight">
            <span className="text-primary">TRACE</span> Dashboard
          </h1>
          <p className="text-sm text-muted-foreground">
            Run <span className="font-mono">{runId}</span>
          </p>
        </header>
        <RunTabs runId={runId} />
      </div>
      {children}
    </>
  );
};

export default RunLayout;
