// GRACE-2 web — spatial-input bus tests (FR-WC-13 / FR-WC-16).
//
// Covers the Chat<->Map sync surface:
//   1. setRequest publishes the active request to subscribers; subscribe fires
//      immediately with the current state (late subscriber paints right away).
//   2. clearRequest only clears the matching request_id (a stale clear is a
//      no-op so a late reply can't wipe a freshly-arrived second request).
//   3. submit relays a completed pick / draw to submit listeners ONLY for the
//      active request_id (stale submit dropped).
//   4. cancel relays a cancellation to cancel listeners ONLY for the active
//      request_id (stale cancel dropped).

import { describe, it, expect, beforeEach } from "vitest";
import { spatialInputBus, type SpatialInputResult } from "./spatial_input_bus";
import type { SpatialInputRequestPayload } from "../contracts";

function req(id: string, mode: SpatialInputRequestPayload["mode"] = "vector_draw"): SpatialInputRequestPayload {
  return {
    request_id: id,
    mode,
    title: "Draw the AOI and any barriers",
    description: "Draw the study area; add walls and flap gates.",
  };
}

const vectorResult = (requestId: string): SpatialInputResult => ({
  requestId,
  geometryType: "vector_draw",
  coordinates: null,
  features: {
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        geometry: { type: "LineString", coordinates: [[-85.305, 35.041], [-85.305, 35.048]] },
        properties: { role: "barrier", barrier_type: "wall" },
      },
    ],
  },
});

describe("spatialInputBus", () => {
  beforeEach(() => {
    spatialInputBus.__reset();
  });

  it("publishes the active request and fires on subscribe", () => {
    const seen: (SpatialInputRequestPayload | null)[] = [];
    const unsub = spatialInputBus.subscribe((st) => seen.push(st.request));
    // Initial fire = null.
    expect(seen).toEqual([null]);
    spatialInputBus.setRequest(req("01REQ0000000000000000000A"));
    expect(seen[seen.length - 1]?.request_id).toBe("01REQ0000000000000000000A");
    unsub();
  });

  it("a late subscriber immediately sees the active request", () => {
    spatialInputBus.setRequest(req("01REQ0000000000000000000B"));
    let got: SpatialInputRequestPayload | null = null;
    const unsub = spatialInputBus.subscribe((st) => {
      got = st.request;
    });
    expect(got).not.toBeNull();
    expect((got as unknown as SpatialInputRequestPayload).request_id).toBe(
      "01REQ0000000000000000000B",
    );
    unsub();
  });

  it("clearRequest only clears the matching request_id", () => {
    spatialInputBus.setRequest(req("01REQ0000000000000000000C"));
    // Stale clear for a different id is a no-op.
    spatialInputBus.clearRequest("01OTHER000000000000000000");
    expect(spatialInputBus.getState().request?.request_id).toBe(
      "01REQ0000000000000000000C",
    );
    // Matching clear wipes it.
    spatialInputBus.clearRequest("01REQ0000000000000000000C");
    expect(spatialInputBus.getState().request).toBeNull();
  });

  it("submit relays only for the active request_id", () => {
    const submits: SpatialInputResult[] = [];
    const unsub = spatialInputBus.subscribeSubmit((r) => submits.push(r));
    // No active request -> dropped.
    spatialInputBus.submit(vectorResult("01REQ0000000000000000000D"));
    expect(submits).toHaveLength(0);
    // Active request -> relayed.
    spatialInputBus.setRequest(req("01REQ0000000000000000000D"));
    spatialInputBus.submit(vectorResult("01REQ0000000000000000000D"));
    expect(submits).toHaveLength(1);
    expect(submits[0]!.geometryType).toBe("vector_draw");
    expect(submits[0]!.features?.features[0]!.properties.barrier_type).toBe("wall");
    // Stale submit for a superseded id -> dropped.
    spatialInputBus.submit(vectorResult("01STALE000000000000000000"));
    expect(submits).toHaveLength(1);
    unsub();
  });

  it("cancel relays only for the active request_id", () => {
    const cancels: string[] = [];
    const unsub = spatialInputBus.subscribeCancel((id) => cancels.push(id));
    // No active request -> dropped.
    spatialInputBus.cancel("01REQ0000000000000000000E");
    expect(cancels).toHaveLength(0);
    spatialInputBus.setRequest(req("01REQ0000000000000000000E", "point"));
    spatialInputBus.cancel("01REQ0000000000000000000E");
    expect(cancels).toEqual(["01REQ0000000000000000000E"]);
    unsub();
  });
});
