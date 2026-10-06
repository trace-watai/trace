"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

interface RunTab {
  label: string;
  /** Path under `/runs/{runId}`; empty for the run's index route. */
  path: string;
  /** Index route is a prefix of every other tab, so it must match exactly. */
  exact?: boolean;
}

/** Ordered tab list; each new run view adds one entry here. */
const TABS: RunTab[] = [
  { label: "Failure card", path: "", exact: true },
  { label: "Trace", path: "/trace" },
  { label: "Verifier", path: "/verifier" },
];

interface RunTabsProps {
  runId: string;
}

/**
 * Tab row shared by every `/runs/[runId]` view. Plain links; this is the only
 * client component, kept as a small leaf because a server layout can't tell
 * which child route is active — `usePathname()` can.
 */
export const RunTabs = ({ runId }: RunTabsProps) => {
  const pathname = usePathname();

  return (
    <nav
      aria-label="Run views"
      className="flex gap-1 border-b border-border/60"
    >
      {TABS.map((tab) => {
        const href = `/runs/${runId}${tab.path}`;
        const isActive = tab.exact
          ? pathname === href
          : pathname.startsWith(href);

        return (
          <Link
            key={tab.label}
            href={href}
            aria-current={isActive ? "page" : undefined}
            className={cn(
              "-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors",
              isActive
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
};
