import { describe, expect, it } from "vitest";

import { evidenceKindLabel } from "@/lib/evidence-kind";
import { EvidenceKind } from "@/types/evidence";

describe("evidenceKindLabel", () => {
  it("has a friendly label for every known kind", () => {
    for (const kind of Object.values(EvidenceKind)) {
      expect(evidenceKindLabel(kind)).not.toContain("_");
    }
  });

  it("renames the jargon kinds", () => {
    expect(evidenceKindLabel("provenance_quote")).toBe("Cited source");
    expect(evidenceKindLabel("retrieval_provenance")).toBe(
      "Retrieved documents",
    );
  });

  it("sentence-cases an unknown kind instead of throwing", () => {
    expect(evidenceKindLabel("some_new_kind")).toBe("Some new kind");
  });
});
