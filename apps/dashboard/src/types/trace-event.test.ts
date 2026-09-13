import { describe, expect, expectTypeOf, it } from "vitest";
import {
  parseTraceEvent,
  TRACE_SCHEMA_VERSION,
  type RawTraceEvent,
} from "@/types/trace-event";

describe("trace event 0.4.0 contract", () => {
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

  it("parses typed retrieval result items", () => {
    const raw: RawTraceEvent<"retrieval_result"> = {
      schema_version: TRACE_SCHEMA_VERSION,
      event_id: "evt_000004",
      run_id: "run_001",
      step_id: 1,
      event_type: "retrieval_result",
      timestamp: "2026-01-01T00:00:02Z",
      payload: {
        query: "refund policy",
        result_count: 1,
        results: [
          {
            doc_id: "refund_policy_v4",
            status: "current",
            title: "Refund Policy v4",
            score: 6.0,
            source: "kb://support/policies/refund_policy_v4",
          },
        ],
      },
      metadata: {},
      parent_event_id: "evt_000003",
    };

    const event = parseTraceEvent(raw);

    expect(event.payload.results).toHaveLength(1);
    const item = event.payload.results[0];
    expect(item.docId).toBe("refund_policy_v4");
    expect(item.status).toBe("current");
    expectTypeOf(item.docId).toEqualTypeOf<string>();
  });

  it("parses blocked_by on a control-blocked tool call", () => {
    const raw: RawTraceEvent<"tool_call_executed"> = {
      schema_version: TRACE_SCHEMA_VERSION,
      event_id: "evt_000005",
      run_id: "run_001",
      step_id: 2,
      event_type: "tool_call_executed",
      timestamp: "2026-01-01T00:00:03Z",
      payload: {
        tool_name: "issue_refund",
        arguments: { refund_type: "cash" },
        status: "error",
        side_effect: "external_irreversible",
        error: "blocked by refund policy guardrail: ...",
        blocked_by: "ctl_refund_window_v1",
      },
      metadata: {},
      parent_event_id: "evt_000004",
    };

    const event = parseTraceEvent(raw);

    expect(event.payload.blockedBy).toBe("ctl_refund_window_v1");
    expect(event.payload.status).toBe("error");
    expectTypeOf(event.payload.blockedBy).toEqualTypeOf<
      string | null | undefined
    >();
  });

  it("accepts pre-0.4.0 tool observations without blocked_by", () => {
    const raw: RawTraceEvent<"tool_observation"> = {
      schema_version: "0.3.0",
      event_id: "evt_000006",
      run_id: "run_001",
      step_id: 2,
      event_type: "tool_observation",
      timestamp: "2026-01-01T00:00:04Z",
      payload: {
        tool_name: "get_order",
        status: "ok",
        result: {},
        error: null,
      },
      metadata: {},
      parent_event_id: "evt_000004",
    };

    expect(parseTraceEvent(raw).payload.blockedBy).toBeUndefined();
  });
});
