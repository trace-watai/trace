import { existsSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { getExperiment, listExperiments } from "@/data/experiment-loader";
import { EXPERIMENT_METRIC_NAMES } from "@/types/experiment";

// The retained baseline lives beside the retained runs, under docs/acceptance.
const ACCEPTANCE = path.join(process.cwd(), "..", "..", "docs", "acceptance");

describe("experiment loader", () => {
  it("reads the retained baseline", () => {
    process.env.TRACE_RUNS_DIR = ACCEPTANCE;
    expect(existsSync(path.join(ACCEPTANCE, "experiments"))).toBe(true);

    const specs = listExperiments();
    expect(specs.length).toBeGreaterThan(0);

    const { spec, result } = getExperiment(specs[0].experimentId);
    expect(spec.frozenManifest.suiteId).toBe("refund_bundles_v0");
    expect(result).not.toBeNull();
    expect(result?.decision).toBe("baseline");
    expect(result?.metrics.verifiedFailureCount).toBe(5);
  });

  it("maps every declared condition to a batch", () => {
    process.env.TRACE_RUNS_DIR = ACCEPTANCE;
    const { spec, result } = getExperiment(listExperiments()[0].experimentId);
    expect(Object.keys(result?.conditionBatches ?? {}).sort()).toEqual(
      spec.conditions.map((c) => c.name).sort(),
    );
  });

  it("exposes the memo's metric names in order", () => {
    expect(EXPERIMENT_METRIC_NAMES).toHaveLength(8);
    expect(EXPERIMENT_METRIC_NAMES[0]).toBe("verdictAgreementRate");
    expect(EXPERIMENT_METRIC_NAMES.at(-1)).toBe("latencyMsP50");
  });
});
