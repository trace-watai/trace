import { describe, expect, expectTypeOf, it } from "vitest";
import {
  parseTraceEvent,
  TRACE_SCHEMA_VERSION,
  type RawTraceEvent,
} from "@/types/trace-event";

describe("trace event 0.2.0 contract", () => {
  it("parses parent_event_id and the event-specific payload", () => {
    const raw: RawTraceEvent<"tool_call_validated"> = {
      schema_version: TRACE_SCHEMA_VERSION,
      event_id: "evt_000002",
      run_id: "run_001",
      step_id: 1,
      event_type: "tool_call_validated",
      timestamp: "2026-01-01T00:00:00Z",
      payload: {
        tool_name: "search_docs",
        valid: true,
        error: null,
      },
      metadata: {},
      parent_event_id: "evt_000001",
    };

    const event = parseTraceEvent(raw);

    expect(event.parentEventId).toBe("evt_000001");
    expect(event.payload).toEqual({
      toolName: "search_docs",
      valid: true,
      error: null,
    });
    expectTypeOf(event.payload.valid).toEqualTypeOf<boolean>();
  });

  it("allows null for top-level parent links", () => {
    const raw: RawTraceEvent<"final_answer"> = {
      schema_version: TRACE_SCHEMA_VERSION,
      event_id: "evt_000003",
      run_id: "run_001",
      step_id: 2,
      event_type: "final_answer",
      timestamp: "2026-01-01T00:00:01Z",
      payload: { final_answer: "Done." },
      metadata: {},
      parent_event_id: null,
    };

    expect(parseTraceEvent(raw).parentEventId).toBeNull();
  });
});
