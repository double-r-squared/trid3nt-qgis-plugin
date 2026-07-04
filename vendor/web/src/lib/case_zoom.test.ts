// GRACE-2 web — Case-open snap-to-location extraction tests (job-0280).
//
// Pins the pure helpers in lib/case_zoom.ts: App.tsx replays the LAST
// `zoom-to` found in a Case's rehydrated chat history through the existing
// LayerPanelBus → Map.tsx fitBounds path. The helper must be defensive —
// persisted rows are typed-loose dicts — and must return null (camera left
// alone) whenever no replayable zoom-to exists.

import { describe, it, expect } from "vitest";
import type { CaseChatMessage, MapCommandPayload } from "../contracts";
import { asBbox, asZoomToCommand, extractLastZoomTo } from "./case_zoom";

// --- Fixtures -------------------------------------------------------------- //

let seq = 0;
function msg(
  emissions: unknown[] | undefined,
  over: Partial<CaseChatMessage> = {},
): CaseChatMessage {
  seq += 1;
  return {
    message_id: `01TESTMSG${String(seq).padStart(17, "0")}`,
    case_id: "01TESTCASE000000000000000",
    role: "agent",
    content: "narration",
    created_at: "2026-06-11T00:00:00Z",
    // typed-loose union on the wire; tests drive the defensive narrowing.
    map_command_emissions: emissions as MapCommandPayload[] | undefined,
    ...over,
  };
}

const FORT_MYERS: [number, number, number, number] = [-82.0, 26.4, -81.7, 26.7];
const BOULDER: [number, number, number, number] = [-105.4, 39.9, -105.1, 40.1];

function zoomTo(bbox: unknown): unknown {
  return { command: "zoom-to", args: { bbox } };
}

// --- asBbox ----------------------------------------------------------------- //

describe("asBbox", () => {
  it("accepts exactly 4 finite numbers", () => {
    expect(asBbox(FORT_MYERS)).toEqual(FORT_MYERS);
    expect(asBbox([0, 0, 0, 0])).toEqual([0, 0, 0, 0]);
  });

  it("rejects wrong arity, non-numbers, and non-finite values", () => {
    expect(asBbox([1, 2, 3])).toBeNull();
    expect(asBbox([1, 2, 3, 4, 5])).toBeNull();
    expect(asBbox(["-82", "26", "-81", "27"])).toBeNull();
    expect(asBbox([NaN, 26, -81, 27])).toBeNull();
    expect(asBbox([Infinity, 26, -81, 27])).toBeNull();
    expect(asBbox(null)).toBeNull();
    expect(asBbox(undefined)).toBeNull();
    expect(asBbox({ bbox: FORT_MYERS })).toBeNull();
  });
});

// --- asZoomToCommand --------------------------------------------------------- //

describe("asZoomToCommand", () => {
  it("narrows the canonical persisted shape {command, args:{bbox}}", () => {
    expect(asZoomToCommand(zoomTo(FORT_MYERS))).toEqual({
      command: "zoom-to",
      args: { bbox: FORT_MYERS },
    });
  });

  it("accepts the flattened defensive shape {command, bbox}", () => {
    expect(
      asZoomToCommand({ command: "zoom-to", bbox: BOULDER }),
    ).toEqual({ command: "zoom-to", args: { bbox: BOULDER } });
  });

  it("rejects other commands, missing/malformed bbox, and non-objects", () => {
    expect(
      asZoomToCommand({ command: "load-layer", args: { bbox: FORT_MYERS } }),
    ).toBeNull();
    expect(asZoomToCommand({ command: "zoom-to" })).toBeNull();
    expect(asZoomToCommand({ command: "zoom-to", args: {} })).toBeNull();
    expect(asZoomToCommand(zoomTo([1, 2]))).toBeNull();
    expect(asZoomToCommand(null)).toBeNull();
    expect(asZoomToCommand("zoom-to")).toBeNull();
    expect(asZoomToCommand(42)).toBeNull();
  });
});

// --- extractLastZoomTo -------------------------------------------------------- //

describe("extractLastZoomTo", () => {
  it("returns null for missing / empty history", () => {
    expect(extractLastZoomTo(undefined)).toBeNull();
    expect(extractLastZoomTo(null)).toBeNull();
    expect(extractLastZoomTo([])).toBeNull();
  });

  it("returns null when no message carries a zoom-to", () => {
    const chat = [
      msg(undefined),
      msg([]),
      msg([{ command: "set-layer-visibility", args: { layer_id: "x", visible: true } }]),
    ];
    expect(extractLastZoomTo(chat)).toBeNull();
  });

  it("finds a single zoom-to and normalizes it to the Map.tsx wire shape", () => {
    const chat = [msg([zoomTo(FORT_MYERS)])];
    expect(extractLastZoomTo(chat)).toEqual({
      command: "zoom-to",
      args: { bbox: FORT_MYERS },
    });
  });

  it("the LAST zoom-to across messages wins (most recent geography)", () => {
    const chat = [
      msg([zoomTo(FORT_MYERS)]),
      msg([{ command: "load-layer" }]),
      msg([zoomTo(BOULDER)]),
      msg(undefined),
    ];
    expect(extractLastZoomTo(chat)?.args.bbox).toEqual(BOULDER);
  });

  it("the LAST zoom-to within one message wins", () => {
    const chat = [msg([zoomTo(FORT_MYERS), zoomTo(BOULDER)])];
    expect(extractLastZoomTo(chat)?.args.bbox).toEqual(BOULDER);
  });

  it("skips malformed entries and falls back to the previous valid one", () => {
    const chat = [
      msg([zoomTo(FORT_MYERS)]),
      msg([
        zoomTo([1, 2, 3]), // wrong arity
        zoomTo(null), // missing bbox
        { command: "zoom-to" }, // no args at all
        null, // junk row
        "zoom-to", // junk row
      ]),
    ];
    expect(extractLastZoomTo(chat)?.args.bbox).toEqual(FORT_MYERS);
  });

  it("tolerates a non-array map_command_emissions field", () => {
    const chat = [
      msg([zoomTo(BOULDER)]),
      msg("not-an-array" as unknown as unknown[]),
    ];
    expect(extractLastZoomTo(chat)?.args.bbox).toEqual(BOULDER);
  });
});
