// GRACE-2 web — routeToolIo reducer tests (tool-card-expand-output spec).
//
// The agent emits a `tool-io` envelope right after each tool dispatch with the
// RAW input args + RAW function_response, keyed by the dispatch's step_id. The
// matching tool card's chevron expands to reveal it. These tests cover the
// stream-routing reducer:
//
//   1. routeToolIo stores the payload keyed by step_id in the OWNING stream.
//   2. it routes to the case-tagged stream over the submit-time target.
//   3. it assigns a fresh Map so React referential-equality detects the change.
//   4. a later dispatch for a different step adds a second entry (no clobber).

import { describe, expect, it } from "vitest";

import {
  createChatStreams,
  getStream,
  routeToolIo,
  routeUserMessage,
  streamKeyFor,
} from "./Chat";
import { ToolIoPayload } from "./contracts";

function io(partial: Partial<ToolIoPayload>): ToolIoPayload {
  return {
    step_id: partial.step_id ?? "step-001",
    tool_name: partial.tool_name ?? "geocode_location",
    raw_args: partial.raw_args ?? '{"location_name": "Boulder, CO"}',
    function_response: partial.function_response ?? '{"status": "ok"}',
    is_error: partial.is_error ?? false,
    args_truncated: partial.args_truncated ?? false,
    response_truncated: partial.response_truncated ?? false,
    args_bytes: partial.args_bytes ?? 32,
    response_bytes: partial.response_bytes ?? 18,
  };
}

describe("routeToolIo", () => {
  it("stores the payload keyed by step_id in the owning stream", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, streamKeyFor("CASE_A"), "geocode boulder");
    routeToolIo(cs, io({ step_id: "s1", raw_args: '{"loc":"a"}' }));

    const s = getStream(cs, "CASE_A");
    expect(s.toolIo.size).toBe(1);
    expect(s.toolIo.get("s1")?.raw_args).toBe('{"loc":"a"}');
  });

  it("routes to the case-tagged stream over the submit-time target", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, streamKeyFor("CASE_A"), "flood in A");
    // A tool-io envelope tagged for a DIFFERENT case lands there.
    routeToolIo(cs, io({ step_id: "s9" }), "CASE_B");

    expect(getStream(cs, "CASE_A").toolIo.size).toBe(0);
    expect(getStream(cs, "CASE_B").toolIo.get("s9")).toBeDefined();
  });

  it("assigns a fresh Map so React referential equality detects the change", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, streamKeyFor("CASE_A"), "x");
    const before = getStream(cs, "CASE_A").toolIo;
    routeToolIo(cs, io({ step_id: "s1" }));
    const after = getStream(cs, "CASE_A").toolIo;
    expect(after).not.toBe(before);
  });

  it("keeps distinct dispatches as separate entries (no clobber)", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, streamKeyFor("CASE_A"), "x");
    routeToolIo(cs, io({ step_id: "s1", tool_name: "geocode_location" }));
    routeToolIo(cs, io({ step_id: "s2", tool_name: "fetch_dem", is_error: true }));

    const s = getStream(cs, "CASE_A");
    expect(s.toolIo.size).toBe(2);
    expect(s.toolIo.get("s1")?.is_error).toBe(false);
    expect(s.toolIo.get("s2")?.is_error).toBe(true);
  });
});
