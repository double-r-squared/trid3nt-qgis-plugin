// GRACE-2 web — Chat region-choice routing + interleave tests
// (state-bbox-fallback narrowing).
//
// Chat itself cannot mount in happy-dom (it opens a WebSocket), so — following
// the established per-Case stream-routing test pattern — these tests exercise
// the exported pure route helpers + buildInterleavedStream directly:
//   - routeRegionChoice lands a RegionChoiceRequestPayload in the owning stream
//     and assigns a chronological arrival seq.
//   - duplicate request_id emits (the session-scoped fan-out can deliver the
//     same envelope twice) do NOT stack a second card.
//   - region-choice cards are per-Case (route to the owning stream; a second
//     Case's stream is untouched).
//   - recordRegionResolved marks the region/whole_state resolution against the
//     stream the card lives in.
//   - buildInterleavedStream places the region-choice card at its first-arrival
//     seq BETWEEN the narration that preceded it and the narration that
//     resumes after — exactly like a credential / tool card.

import { describe, it, expect } from "vitest";
import {
  ROOT_STREAM_KEY,
  createChatStreams,
  getStream,
  routeUserMessage,
  routeAgentChunk,
  routePipelineState,
  routeRegionChoice,
  recordRegionResolved,
  buildInterleavedStream,
} from "./Chat";
import {
  PipelineStatePayload,
  RegionChoiceRequestPayload,
} from "./contracts";

const CASE_A = "01CASEAAAAAAAAAAAAAAAAAAAA";
const CASE_B = "01CASEBBBBBBBBBBBBBBBBBBBB";

function req(
  requestId: string,
  overrides: Partial<RegionChoiceRequestPayload> = {},
): RegionChoiceRequestPayload {
  return {
    envelope_type: "region-choice-request",
    request_id: requestId,
    state_name: "Florida",
    state_code: "FL",
    state_bbox: [-87.6, 24.5, -80.0, 31.0],
    candidates: [
      {
        region_id: "county-12071",
        name: "Lee County",
        bbox: [-82.3, 26.3, -81.6, 26.8],
        admin_level: "county",
      },
    ],
    default_action: "use_whole_state",
    message: "'south Florida' isn't a precise place — pick an area in Florida.",
    ...overrides,
  };
}

describe("routeRegionChoice — region picker routing", () => {
  it("lands a region-choice request in the owning stream with an arrival seq", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "model a flood in south Florida");
    routeRegionChoice(cs, req("R1"));
    const s = getStream(cs, CASE_A);
    expect(s.regionChoices.map((r) => r.request_id)).toEqual(["R1"]);
    expect(s.regionSeqs.get("R1")).toBeGreaterThan(0);
  });

  it("de-dupes a duplicate request_id (session-scoped fan-out can repeat)", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "hi");
    routeRegionChoice(cs, req("R1"));
    routeRegionChoice(cs, req("R1"));
    expect(getStream(cs, CASE_A).regionChoices).toHaveLength(1);
  });

  it("routes to the OWNING stream; another Case is untouched", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "A prompt");
    routeRegionChoice(cs, req("R1"));
    expect(getStream(cs, CASE_A).regionChoices).toHaveLength(1);
    expect(getStream(cs, CASE_B).regionChoices).toHaveLength(0);
    expect(getStream(cs, ROOT_STREAM_KEY).regionChoices).toHaveLength(0);
  });

  it("explicit caseId targeting overrides the in-flight targetKey", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "A prompt");
    routeRegionChoice(cs, req("R2"), CASE_B);
    expect(getStream(cs, CASE_A).regionChoices).toHaveLength(0);
    expect(
      getStream(cs, CASE_B).regionChoices.map((r) => r.request_id),
    ).toEqual(["R2"]);
  });
});

describe("recordRegionResolved — region / whole_state", () => {
  it("marks a request narrowed to a region against its stream", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "hi");
    routeRegionChoice(cs, req("R1"));
    recordRegionResolved(cs, CASE_A, "R1", "region", "county-12071");
    expect(getStream(cs, CASE_A).regionResolved.get("R1")).toEqual({
      choice: "region",
      regionId: "county-12071",
    });
  });

  it("marks a request as whole_state (regionId null) against its stream", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "hi");
    routeRegionChoice(cs, req("R1"));
    recordRegionResolved(cs, CASE_A, "R1", "whole_state", null);
    expect(getStream(cs, CASE_A).regionResolved.get("R1")).toEqual({
      choice: "whole_state",
      regionId: null,
    });
  });
});

describe("buildInterleavedStream — region-choice card interleave", () => {
  it("places the region-choice card between preceding + following narration", () => {
    const cs = createChatStreams();
    // user prompt → agent "locating…" → region-choice request → agent resumes.
    routeUserMessage(cs, CASE_A, "model a flood in south Florida");
    routeAgentChunk(cs, {
      message_id: "m1",
      delta: "I'm locating the area...",
      done: true,
    });
    routeRegionChoice(cs, req("R1"));
    routeAgentChunk(cs, {
      message_id: "m2",
      delta: "Great — using Lee County.",
      done: true,
    });
    const s = getStream(cs, CASE_A);
    const stream = buildInterleavedStream(
      s.messages,
      s.pipeline.history,
      s.pipeline.live,
      s.messageOrder,
      s.stepOrder,
      [],
      new Map(),
      new Map(),
      [],
      new Map(),
      new Map(),
      s.regionChoices,
      s.regionSeqs,
      s.regionResolved,
    );
    const kinds = stream.map((e) => e.kind);
    // user → agent("locating") → region-choice → agent("resumes").
    expect(kinds).toEqual([
      "user-message",
      "agent-message",
      "region-choice",
      "agent-message",
    ]);
    const rc = stream.find((e) => e.kind === "region-choice");
    expect(rc && rc.kind === "region-choice" ? rc.requestId : null).toBe("R1");
  });

  it("interleaves a tool card AND the region-choice card by arrival order", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "flood south Florida");
    // A geocode tool card lands first, then the region-choice request.
    const snap: PipelineStatePayload = {
      pipeline_id: "p1",
      steps: [
        {
          step_id: "s1",
          name: "geocode_location",
          tool_name: "geocode_location",
          state: "complete",
        },
      ],
    };
    routePipelineState(cs, snap);
    routeRegionChoice(cs, req("R1"));
    const s = getStream(cs, CASE_A);
    const stream = buildInterleavedStream(
      s.messages,
      s.pipeline.history,
      s.pipeline.live,
      s.messageOrder,
      s.stepOrder,
      [],
      new Map(),
      new Map(),
      [],
      new Map(),
      new Map(),
      s.regionChoices,
      s.regionSeqs,
      s.regionResolved,
    );
    expect(stream.map((e) => e.kind)).toEqual([
      "user-message",
      "tool",
      "region-choice",
    ]);
  });

  it("carries the resolved choice + regionId into the entry view-model", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "hi");
    routeRegionChoice(cs, req("R1"));
    recordRegionResolved(cs, CASE_A, "R1", "region", "county-12071");
    const s = getStream(cs, CASE_A);
    const stream = buildInterleavedStream(
      s.messages,
      s.pipeline.history,
      s.pipeline.live,
      s.messageOrder,
      s.stepOrder,
      [],
      new Map(),
      new Map(),
      [],
      new Map(),
      new Map(),
      s.regionChoices,
      s.regionSeqs,
      s.regionResolved,
    );
    const rc = stream.find((e) => e.kind === "region-choice");
    expect(rc && rc.kind === "region-choice" ? rc.resolved : null).toEqual({
      choice: "region",
      regionId: "county-12071",
    });
  });
});
