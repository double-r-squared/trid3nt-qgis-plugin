// GRACE-2 web — ws.ts spatial-input wire tests (FR-WC-13 pick-mode + FR-WC-16
// urban vector-draw).
//
// Verifies the spatial-input plumbing added alongside the region-choice seam:
//   1. A `spatial-input-request` envelope dispatches to onSpatialInputRequest.
//   2. A malformed request (missing request_id) is dropped, not forwarded.
//   3. The optional handler contract: no throw when no handler is provided.
//   4. `sendSpatialInputResponse` serializes a vector_draw FeatureCollection
//      reply (role-tagged + per-segment barrier_type + flap_direction) onto the
//      wire, AND a cancellation nulls the geometry fields.
//   5. `spatial-input-request` is session-scoped (fans out to a sibling GraceWs
//      bound to the same session_id) — mirrors the region-choice rationale.
//
// WebSocket is driven via happy-dom's stub + MessageEvent injection, matching
// the existing ws.test.tsx harness.

import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  GraceWs,
  __test_resetSessionHub,
  type WsHandlers,
} from "./ws";
import type {
  SpatialDrawFeatureCollection,
  SpatialInputRequestPayload,
} from "./contracts";

function makeHandlers(overrides: Partial<WsHandlers> = {}): WsHandlers {
  return {
    onStatus: vi.fn(),
    onAgentChunk: vi.fn(),
    onPipelineState: vi.fn(),
    onSessionState: vi.fn(),
    onError: vi.fn(),
    ...overrides,
  };
}

function makeEnvelope(type: string, payload: unknown): string {
  return JSON.stringify({
    type,
    id: "01ABCDEFGHJKMNPQRSTVWX0001",
    ts: "2026-06-17T21:00:00.000Z",
    session_id: "01ABCDEFGHJKMNPQRSTVWX0002",
    payload,
  });
}

function lastOpenedSocket(): WebSocket | null {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const sockets = (window as any).__webSockets as WebSocket[] | undefined;
  if (!sockets || sockets.length === 0) return null;
  return sockets[sockets.length - 1]!;
}

function injectMessage(ws: WebSocket, raw: string): void {
  ws.dispatchEvent(new MessageEvent("message", { data: raw }));
}

/** A drawn FeatureCollection: AOI polygon + a wall + a flap gate + a point. */
function vectorDrawFeatureCollection(): SpatialDrawFeatureCollection {
  return {
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        geometry: {
          type: "Polygon",
          coordinates: [
            [
              [-85.31, 35.04],
              [-85.30, 35.04],
              [-85.30, 35.05],
              [-85.31, 35.05],
              [-85.31, 35.04],
            ],
          ],
        },
        properties: { role: "aoi" },
      },
      {
        type: "Feature",
        geometry: {
          type: "LineString",
          coordinates: [
            [-85.305, 35.041],
            [-85.305, 35.048],
          ],
        },
        properties: { role: "barrier", barrier_type: "wall" },
      },
      {
        type: "Feature",
        geometry: {
          type: "LineString",
          coordinates: [
            [-85.308, 35.043],
            [-85.302, 35.043],
          ],
        },
        properties: {
          role: "barrier",
          barrier_type: "flap_gate",
          flap_direction: "out",
          protected_side: "left",
        },
      },
    ],
  };
}

describe("GraceWs — spatial-input dispatch (FR-WC-13 / FR-WC-16)", () => {
  beforeEach(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).__webSockets = [];
    __test_resetSessionHub();
  });

  it("dispatches a vector_draw request to onSpatialInputRequest", () => {
    const onSpatialInputRequest =
      vi.fn<(p: SpatialInputRequestPayload) => void>();
    const ws = new GraceWs(
      "ws://localhost:8765",
      makeHandlers({ onSpatialInputRequest }),
    );
    ws.connect();
    const socket = lastOpenedSocket();
    if (!socket) {
      ws.close();
      return;
    }

    const payload = {
      request_id: "01ABCDEFGHJKMNPQRSTVWX0003",
      mode: "vector_draw",
      title: "Draw the AOI and any barriers",
      description: "Draw the study area; add walls (red) and flap gates (green).",
      suggested_view: { bbox: [-85.31, 35.04, -85.30, 35.05], zoom: 15.0 },
    };
    injectMessage(socket, makeEnvelope("spatial-input-request", payload));

    expect(onSpatialInputRequest).toHaveBeenCalledOnce();
    const received = onSpatialInputRequest.mock.calls[0]![0];
    expect(received.mode).toBe("vector_draw");
    expect(received.request_id).toBe("01ABCDEFGHJKMNPQRSTVWX0003");

    ws.close();
  });

  it("drops a request missing request_id (does not forward)", () => {
    const onSpatialInputRequest =
      vi.fn<(p: SpatialInputRequestPayload) => void>();
    const ws = new GraceWs(
      "ws://localhost:8765",
      makeHandlers({ onSpatialInputRequest }),
    );
    ws.connect();
    const socket = lastOpenedSocket();
    if (!socket) {
      ws.close();
      return;
    }

    injectMessage(
      socket,
      makeEnvelope("spatial-input-request", {
        mode: "point",
        title: "t",
        description: "d",
      }),
    );
    expect(onSpatialInputRequest).not.toHaveBeenCalled();

    ws.close();
  });

  it("does not throw when onSpatialInputRequest is not provided", () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.connect();
    const socket = lastOpenedSocket();
    if (!socket) {
      ws.close();
      return;
    }
    expect(() =>
      injectMessage(
        socket,
        makeEnvelope("spatial-input-request", {
          request_id: "01ABCDEFGHJKMNPQRSTVWX0003",
          mode: "vector_draw",
          title: "t",
          description: "d",
        }),
      ),
    ).not.toThrow();

    ws.close();
  });
});

describe("GraceWs — sendSpatialInputResponse (FR-WC-16)", () => {
  beforeEach(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).__webSockets = [];
    __test_resetSessionHub();
  });

  it("serializes a vector_draw FeatureCollection reply with per-segment tags", () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.connect();
    const socket = lastOpenedSocket();
    if (!socket) {
      ws.close();
      return;
    }
    Object.defineProperty(socket, "readyState", {
      configurable: true,
      get: () => 1, // WebSocket.OPEN
    });
    const sendSpy = vi
      .spyOn(socket, "send")
      .mockImplementation(() => undefined);

    const fc = vectorDrawFeatureCollection();
    ws.sendSpatialInputResponse({
      request_id: "01ABCDEFGHJKMNPQRSTVWX0003",
      geometry_type: "vector_draw",
      features: fc,
    });

    expect(sendSpy).toHaveBeenCalledOnce();
    const wire = JSON.parse(sendSpy.mock.calls[0]![0] as string) as {
      type: string;
      session_id: string;
      payload: {
        envelope_type?: string;
        request_id: string;
        geometry_type?: string | null;
        coordinates?: number[] | null;
        features?: SpatialDrawFeatureCollection | null;
        cancelled?: boolean;
      };
    };
    expect(wire.type).toBe("spatial-input-response");
    expect(wire.session_id).toBe(ws.session);
    expect(wire.payload.envelope_type).toBe("spatial-input-response");
    expect(wire.payload.geometry_type).toBe("vector_draw");
    expect(wire.payload.coordinates).toBeNull();
    expect(wire.payload.cancelled).toBe(false);
    const feats = wire.payload.features!.features;
    const roles = feats.map((f) => f.properties.role);
    expect(roles).toEqual(["aoi", "barrier", "barrier"]);
    const barriers = feats.filter((f) => f.properties.role === "barrier");
    expect(barriers.map((b) => b.properties.barrier_type)).toEqual([
      "wall",
      "flap_gate",
    ]);
    const flap = barriers.find(
      (b) => b.properties.barrier_type === "flap_gate",
    )!;
    expect(flap.properties.flap_direction).toBe("out");
    expect(flap.properties.protected_side).toBe("left");

    ws.close();
  });

  it("nulls geometry fields on a cancellation", () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    ws.connect();
    const socket = lastOpenedSocket();
    if (!socket) {
      ws.close();
      return;
    }
    Object.defineProperty(socket, "readyState", {
      configurable: true,
      get: () => 1,
    });
    const sendSpy = vi
      .spyOn(socket, "send")
      .mockImplementation(() => undefined);

    ws.sendSpatialInputResponse({
      request_id: "01ABCDEFGHJKMNPQRSTVWX0003",
      geometry_type: "vector_draw",
      features: vectorDrawFeatureCollection(),
      cancelled: true,
    });

    const wire = JSON.parse(sendSpy.mock.calls[0]![0] as string) as {
      payload: {
        geometry_type?: string | null;
        coordinates?: number[] | null;
        features?: unknown;
        cancelled?: boolean;
      };
    };
    expect(wire.payload.cancelled).toBe(true);
    expect(wire.payload.geometry_type).toBeNull();
    expect(wire.payload.coordinates).toBeNull();
    expect(wire.payload.features).toBeNull();

    ws.close();
  });

  it("no-ops (no throw) when the socket is not open", () => {
    const ws = new GraceWs("ws://localhost:8765", makeHandlers());
    expect(() =>
      ws.sendSpatialInputResponse({
        request_id: "01ABCDEFGHJKMNPQRSTVWX0003",
        geometry_type: "point",
        coordinates: [-85.3, 35.0],
      }),
    ).not.toThrow();
  });
});

describe("GraceWs — spatial-input-request session-scoped fan-out", () => {
  beforeEach(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).__webSockets = [];
    __test_resetSessionHub();
  });

  it("fans a spatial-input-request out to a sibling GraceWs", () => {
    const chatHandler = vi.fn<(p: SpatialInputRequestPayload) => void>();
    const appHandler = vi.fn<(p: SpatialInputRequestPayload) => void>();
    const chat = new GraceWs(
      "ws://localhost:8765",
      makeHandlers({ onSpatialInputRequest: chatHandler }),
    );
    const app = new GraceWs(
      "ws://localhost:8765",
      makeHandlers({ onSpatialInputRequest: appHandler }),
    );
    expect(chat.session).toBe(app.session);

    chat.connect();
    app.connect();
    const sockets = (window as unknown as { __webSockets?: WebSocket[] })
      .__webSockets;
    if (!sockets || sockets.length < 2) {
      chat.close();
      app.close();
      return;
    }
    const chatSocket = sockets[sockets.length - 2]!;
    injectMessage(
      chatSocket,
      makeEnvelope("spatial-input-request", {
        request_id: "01ABCDEFGHJKMNPQRSTVWX0003",
        mode: "vector_draw",
        title: "Draw the AOI",
        description: "d",
      }),
    );

    // Chat sees it natively; App sees it via the fan-out hub (the urban-flood
    // tool may pause on Chat's socket while the draw surface lives on App's).
    expect(chatHandler).toHaveBeenCalledOnce();
    expect(appHandler).toHaveBeenCalledOnce();

    chat.close();
    app.close();
  });
});
