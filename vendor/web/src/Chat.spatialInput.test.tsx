// GRACE-2 web — Chat spatial-input routing + interleave tests
// (FR-WC-13 pick-mode + FR-WC-16 urban vector-draw).
//
// Chat itself cannot mount in happy-dom (it opens a WebSocket), so — following
// the established per-Case stream-routing test pattern (Chat.regionChoice.test)
// — these tests exercise the exported pure route helpers + buildInterleavedStream
// directly:
//   - routeSpatialInput lands a SpatialInputRequestPayload in the owning stream
//     with a chronological arrival seq.
//   - duplicate request_id emits (session-scoped fan-out can deliver twice) do
//     NOT stack a second card.
//   - spatial-input cards are per-Case (route to the owning stream).
//   - recordSpatialResolved marks submitted / cancelled against the stream.
//   - buildInterleavedStream places the spatial-input card at its first-arrival
//     seq BETWEEN the narration before and after — like a region-choice card.

import { describe, it, expect } from "vitest";
import {
  ROOT_STREAM_KEY,
  createChatStreams,
  getStream,
  routeUserMessage,
  routeAgentChunk,
  routeSpatialInput,
  recordSpatialResolved,
  buildInterleavedStream,
} from "./Chat";
import type { SpatialInputRequestPayload } from "./contracts";

const CASE_A = "01CASEAAAAAAAAAAAAAAAAAAAA";
const CASE_B = "01CASEBBBBBBBBBBBBBBBBBBBB";

function req(
  requestId: string,
  mode: SpatialInputRequestPayload["mode"] = "vector_draw",
): SpatialInputRequestPayload {
  return {
    envelope_type: "spatial-input-request",
    request_id: requestId,
    mode,
    title: "Draw the AOI and any barriers",
    description: "Draw the study area; add walls (red) and flap gates (green).",
    suggested_view: { bbox: [-85.31, 35.04, -85.30, 35.05], zoom: 15 },
  };
}

// buildInterleavedStream's spatial-input args are positional after the
// region-choice trio — pass empties for the credential / payload / region slots.
function build(s: ReturnType<typeof getStream>) {
  return buildInterleavedStream(
    s.messages,
    s.pipeline.history,
    s.pipeline.live,
    s.messageOrder,
    s.stepOrder,
    [], // credentialRequests
    new Map(),
    new Map(),
    [], // payloadWarnings
    new Map(),
    new Map(),
    [], // regionChoices
    new Map(),
    new Map(),
    s.spatialInputs,
    s.spatialSeqs,
    s.spatialResolved,
  );
}

describe("routeSpatialInput — spatial-input routing", () => {
  it("lands a spatial-input request in the owning stream with an arrival seq", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "model urban flooding downtown");
    routeSpatialInput(cs, req("S1"));
    const s = getStream(cs, CASE_A);
    expect(s.spatialInputs.map((r) => r.request_id)).toEqual(["S1"]);
    expect(s.spatialSeqs.get("S1")).toBeGreaterThan(0);
  });

  it("de-dupes a duplicate request_id (session-scoped fan-out can repeat)", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "hi");
    routeSpatialInput(cs, req("S1"));
    routeSpatialInput(cs, req("S1"));
    expect(getStream(cs, CASE_A).spatialInputs).toHaveLength(1);
  });

  it("routes to the OWNING stream; another Case is untouched", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "A prompt");
    routeSpatialInput(cs, req("S1"));
    expect(getStream(cs, CASE_A).spatialInputs).toHaveLength(1);
    expect(getStream(cs, CASE_B).spatialInputs).toHaveLength(0);
    expect(getStream(cs, ROOT_STREAM_KEY).spatialInputs).toHaveLength(0);
  });

  it("explicit caseId targeting overrides the in-flight targetKey", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "A prompt");
    routeSpatialInput(cs, req("S2"), CASE_B);
    expect(getStream(cs, CASE_A).spatialInputs).toHaveLength(0);
    expect(getStream(cs, CASE_B).spatialInputs.map((r) => r.request_id)).toEqual([
      "S2",
    ]);
  });
});

describe("recordSpatialResolved — submitted / cancelled", () => {
  it("marks a request submitted against its stream", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "hi");
    routeSpatialInput(cs, req("S1"));
    recordSpatialResolved(cs, CASE_A, "S1", "submitted");
    expect(getStream(cs, CASE_A).spatialResolved.get("S1")).toBe("submitted");
  });

  it("marks a request cancelled against its stream", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "hi");
    routeSpatialInput(cs, req("S1"));
    recordSpatialResolved(cs, CASE_A, "S1", "cancelled");
    expect(getStream(cs, CASE_A).spatialResolved.get("S1")).toBe("cancelled");
  });
});

describe("buildInterleavedStream — spatial-input card interleave", () => {
  it("places the spatial-input card between preceding + following narration", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "model urban flooding with barriers");
    routeAgentChunk(cs, {
      message_id: "m1",
      delta: "Draw the area and any flood barriers...",
      done: true,
    });
    routeSpatialInput(cs, req("S1"));
    routeAgentChunk(cs, {
      message_id: "m2",
      delta: "Got it — running the urban-flood model.",
      done: true,
    });
    const s = getStream(cs, CASE_A);
    const stream = build(s);
    expect(stream.map((e) => e.kind)).toEqual([
      "user-message",
      "agent-message",
      "spatial-input",
      "agent-message",
    ]);
    const si = stream.find((e) => e.kind === "spatial-input");
    expect(si && si.kind === "spatial-input" ? si.requestId : null).toBe("S1");
  });

  it("carries the resolved state into the entry view-model", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "hi");
    routeSpatialInput(cs, req("S1"));
    recordSpatialResolved(cs, CASE_A, "S1", "submitted");
    const s = getStream(cs, CASE_A);
    const stream = build(s);
    const si = stream.find((e) => e.kind === "spatial-input");
    expect(si && si.kind === "spatial-input" ? si.resolved : null).toBe(
      "submitted",
    );
  });
});
