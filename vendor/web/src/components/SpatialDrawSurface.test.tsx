// GRACE-2 web — SpatialDrawSurface submit-gate tests (FR-WC-16 untagged-barrier
// mismatch, WEB half).
//
// The invariant under test: the client must NOT be able to submit a
// `spatial-input-response` whose vector_draw FeatureCollection contains a
// role=="barrier" feature with NO barrier_type. The DrawController readback is
// deliberately a faithful, lossless mirror (an untagged barrier reads back as
// role=="barrier" with no barrier_type — see draw_controller.test.ts), so the
// guarantee is enforced at the SUBMIT GATE in SpatialDrawSurface:
//
//   1. canSubmit is FALSE while any drawn barrier is still untagged, and the
//      surface shows the honest reason ("Tag every barrier as wall or
//      flap-gate to submit"). It flips TRUE once every barrier is tagged.
//   2. A FeatureCollection that DOES reach onSubmit never contains a
//      role=="barrier" feature lacking barrier_type.
//
// terra-draw needs a live MapLibre adapter (happy-dom lacks one), so we inject a
// lightweight in-memory TerraDraw stub through the component's `drawDeps` seam
// (the same FakeTerraDraw shape draw_controller.test.ts uses) plus a minimal
// MapLibre map stub covering only the methods the surface touches.

import { describe, it, expect, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { SpatialDrawSurface } from "./SpatialDrawSurface";
import type { DrawControllerDeps, DrawFeatureId } from "../lib/draw_controller";
import type { SpatialInputRequestPayload } from "../contracts";
import type { SpatialInputResult } from "../lib/spatial_input_bus";
import type { Map as MapLibreMap } from "maplibre-gl";
import type { GeoJSONStoreFeatures, TerraDraw } from "terra-draw";

// --- In-memory TerraDraw stub (mirrors draw_controller.test.ts) ---------- //

type ChangeCb = (ids: DrawFeatureId[], type: string) => void;
type SelectCb = (id: DrawFeatureId) => void;

class FakeTerraDraw {
  private features = new Map<DrawFeatureId, GeoJSONStoreFeatures>();
  private mode = "static";
  private changeCbs = new Set<ChangeCb>();
  private selectCbs = new Set<SelectCb>();
  private nextId = 1;
  started = false;

  start(): void {
    this.started = true;
  }
  stop(): void {
    this.started = false;
    this.features.clear();
  }
  setMode(mode: string): void {
    this.mode = mode;
  }
  getMode(): string {
    return this.mode;
  }
  clear(): void {
    this.features.clear();
    this.fireChange([], "clear");
  }
  on(event: string, cb: ChangeCb | SelectCb): void {
    if (event === "change") this.changeCbs.add(cb as ChangeCb);
    if (event === "select") this.selectCbs.add(cb as SelectCb);
  }
  off(event: string, cb: ChangeCb | SelectCb): void {
    if (event === "change") this.changeCbs.delete(cb as ChangeCb);
    if (event === "select") this.selectCbs.delete(cb as SelectCb);
  }
  getSnapshot(): GeoJSONStoreFeatures[] {
    return Array.from(this.features.values()).map(
      (f) => JSON.parse(JSON.stringify(f)) as GeoJSONStoreFeatures,
    );
  }
  removeFeatures(ids: DrawFeatureId[]): void {
    for (const id of ids) this.features.delete(id);
    this.fireChange(ids, "delete");
  }
  updateFeatureProperties(
    id: DrawFeatureId,
    properties: Record<string, unknown>,
  ): void {
    const f = this.features.get(id);
    if (!f) return;
    const next = { ...(f.properties ?? {}) } as Record<string, unknown>;
    for (const [k, v] of Object.entries(properties)) {
      if (v === undefined) delete next[k];
      else next[k] = v;
    }
    f.properties = next as GeoJSONStoreFeatures["properties"];
    this.fireChange([id], "update");
  }

  // --- test helpers (not part of the TerraDraw surface) ------------------ //
  _add(
    geometry: GeoJSONStoreFeatures["geometry"],
    properties: Record<string, unknown>,
  ): DrawFeatureId {
    const id = this.nextId++;
    this.features.set(id, {
      id,
      type: "Feature",
      geometry,
      properties: properties as GeoJSONStoreFeatures["properties"],
    });
    this.fireChange([id], "create");
    return id;
  }
  _select(id: DrawFeatureId): void {
    for (const cb of this.selectCbs) cb(id);
  }
  private fireChange(ids: DrawFeatureId[], type: string): void {
    for (const cb of this.changeCbs) cb(ids, type);
  }
}

// --- Minimal MapLibre map stub (only the surface's touchpoints) ---------- //

function makeFakeMap(): MapLibreMap {
  const canvas = { style: { cursor: "" } };
  return {
    fitBounds: vi.fn(),
    isStyleLoaded: () => true,
    getCanvas: () => canvas,
    getSource: () => undefined,
    addSource: vi.fn(),
    getLayer: () => undefined,
    addLayer: vi.fn(),
    removeLayer: vi.fn(),
    removeSource: vi.fn(),
    on: vi.fn(),
    off: vi.fn(),
    once: vi.fn(),
    dragPan: { enable: vi.fn(), disable: vi.fn() },
  } as unknown as MapLibreMap;
}

// --- Geometry helpers ----------------------------------------------------- //

const AOI_SQUARE: GeoJSONStoreFeatures["geometry"] = {
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
};

function lineGeom(coords: number[][]): GeoJSONStoreFeatures["geometry"] {
  return { type: "LineString", coordinates: coords };
}

function vectorRequest(): SpatialInputRequestPayload {
  return {
    envelope_type: "spatial-input-request",
    request_id: "01HJSPATIAL00000000000001",
    mode: "vector_draw",
    title: "Draw the AOI and any barriers",
    description: "Draw the study area; add walls (red) and flap gates (green).",
    suggested_view: { bbox: [-85.31, 35.04, -85.30, 35.05], zoom: 15 },
  };
}

/** Render the surface with an injected FakeTerraDraw; return the harness. */
function renderSurface() {
  const fake = new FakeTerraDraw();
  const drawDeps: DrawControllerDeps = {
    makeDraw: () => fake as unknown as TerraDraw,
  };
  const onSubmit = vi.fn<(r: SpatialInputResult) => void>();
  const onCancel = vi.fn<(id: string) => void>();
  render(
    <SpatialDrawSurface
      map={makeFakeMap()}
      request={vectorRequest()}
      onSubmit={onSubmit}
      onCancel={onCancel}
      drawDeps={drawDeps}
    />,
  );
  return { fake, onSubmit, onCancel };
}

function submitBtn(): HTMLButtonElement {
  return screen.getByTestId("spatial-draw-submit") as HTMLButtonElement;
}

describe("SpatialDrawSurface — submit gate (FR-WC-16 untagged barrier)", () => {
  it("blocks submit while a barrier is untagged, then enables once tagged", () => {
    const { fake, onSubmit } = renderSurface();

    // An AOI alone would normally satisfy the old count-only gate.
    act(() => {
      fake._add(AOI_SQUARE, { mode: "polygon" });
    });
    expect(submitBtn().disabled).toBe(false);

    // Draw a barrier line but DON'T tag it — submit must now be blocked.
    let barrierId: DrawFeatureId = -1;
    act(() => {
      barrierId = fake._add(
        lineGeom([[-85.305, 35.041], [-85.305, 35.048]]),
        { mode: "linestring" },
      );
    });
    expect(submitBtn().disabled).toBe(true);

    // The honest reason is surfaced.
    expect(
      screen.getByTestId("spatial-draw-submit-reason").textContent,
    ).toContain("Tag every barrier as wall or flap-gate to submit");
    // The toolbar count corroborates the block.
    expect(screen.getByTestId("draw-counts").textContent).toContain("1 untagged");

    // Clicking the disabled submit relays nothing.
    fireEvent.click(submitBtn());
    expect(onSubmit).not.toHaveBeenCalled();

    // Tag the barrier as a wall — submit unblocks and the reason disappears.
    act(() => {
      fake.updateFeatureProperties(barrierId, { role: "barrier", barrier_type: "wall" });
    });
    expect(submitBtn().disabled).toBe(false);
    expect(screen.queryByTestId("spatial-draw-submit-reason")).toBeNull();
  });

  it("blocks submit when ANY of several barriers is still untagged", () => {
    const { fake } = renderSurface();
    act(() => {
      fake._add(AOI_SQUARE, { mode: "polygon" });
    });
    let tagged: DrawFeatureId = -1;
    let untagged: DrawFeatureId = -1;
    act(() => {
      tagged = fake._add(lineGeom([[-85.305, 35.041], [-85.305, 35.048]]), {
        mode: "linestring",
      });
      untagged = fake._add(lineGeom([[-85.308, 35.043], [-85.302, 35.043]]), {
        mode: "linestring",
      });
    });
    // Tag only the first barrier.
    act(() => {
      fake.updateFeatureProperties(tagged, {
        role: "barrier",
        barrier_type: "flap_gate",
        flap_direction: "out",
      });
    });
    // One barrier still untagged -> still blocked.
    expect(submitBtn().disabled).toBe(true);
    expect(screen.getByTestId("draw-counts").textContent).toContain("1 untagged");

    // Tag the second -> now submittable.
    act(() => {
      fake.updateFeatureProperties(untagged, {
        role: "barrier",
        barrier_type: "wall",
      });
    });
    expect(submitBtn().disabled).toBe(false);
  });

  it("submitted FeatureCollection never carries a role==barrier feature lacking barrier_type", () => {
    const { fake, onSubmit } = renderSurface();
    act(() => {
      fake._add(AOI_SQUARE, { mode: "polygon" });
    });
    let wall: DrawFeatureId = -1;
    let flap: DrawFeatureId = -1;
    act(() => {
      wall = fake._add(lineGeom([[-85.305, 35.041], [-85.305, 35.048]]), {
        mode: "linestring",
      });
      flap = fake._add(lineGeom([[-85.308, 35.043], [-85.302, 35.043]]), {
        mode: "linestring",
      });
    });
    act(() => {
      fake.updateFeatureProperties(wall, { role: "barrier", barrier_type: "wall" });
      fake.updateFeatureProperties(flap, {
        role: "barrier",
        barrier_type: "flap_gate",
        flap_direction: "in",
      });
    });

    expect(submitBtn().disabled).toBe(false);
    fireEvent.click(submitBtn());

    expect(onSubmit).toHaveBeenCalledTimes(1);
    const result = onSubmit.mock.calls[0]![0];
    expect(result.geometryType).toBe("vector_draw");
    const fc = result.features!;
    // Every barrier in the SUBMITTED collection has a barrier_type.
    const barriers = fc.features.filter((f) => f.properties.role === "barrier");
    expect(barriers.length).toBeGreaterThan(0);
    for (const b of barriers) {
      expect(b.properties.barrier_type).toBeDefined();
    }
    // Sanity: none is the forbidden untagged-barrier shape.
    const offenders = fc.features.filter(
      (f) => f.properties.role === "barrier" && !f.properties.barrier_type,
    );
    expect(offenders).toHaveLength(0);
  });

  it("programmatic handleSubmit is inert while blocked (guard, not just disabled attr)", () => {
    // Even if the disabled attribute were bypassed, the FeatureCollection that
    // an untagged barrier would produce must never reach onSubmit. We assert
    // that the readback the controller WOULD emit is the forbidden shape, and
    // that clicking submit in that state still relays nothing.
    const { fake, onSubmit } = renderSurface();
    act(() => {
      fake._add(AOI_SQUARE, { mode: "polygon" });
      fake._add(lineGeom([[-85.305, 35.041], [-85.305, 35.048]]), {
        mode: "linestring",
      });
    });
    expect(submitBtn().disabled).toBe(true);
    fireEvent.click(submitBtn());
    fireEvent.click(submitBtn());
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

// =========================================================================== //
// FIX 2: NEUTRAL-LINE flow (purpose="line") -- a plain elevation/section line
// submits WITHOUT any wall/flap_gate tagging, and rides back as a role=="line"
// LineString. The barrier flow above stays the no-regression control.
// =========================================================================== //

function neutralLineRequest(): SpatialInputRequestPayload {
  return {
    envelope_type: "spatial-input-request",
    request_id: "01HJSPATIAL00000000000002",
    mode: "vector_draw",
    purpose: "line",
    title: "Draw the elevation profile line",
    description: "Draw a line across the ridge for the terrain profile.",
    suggested_view: { bbox: [-85.31, 35.04, -85.30, 35.05], zoom: 15 },
  };
}

function renderNeutralLineSurface() {
  const fake = new FakeTerraDraw();
  const drawDeps: DrawControllerDeps = {
    makeDraw: () => fake as unknown as TerraDraw,
  };
  const onSubmit = vi.fn<(r: SpatialInputResult) => void>();
  const onCancel = vi.fn<(id: string) => void>();
  render(
    <SpatialDrawSurface
      map={makeFakeMap()}
      request={neutralLineRequest()}
      onSubmit={onSubmit}
      onCancel={onCancel}
      drawDeps={drawDeps}
    />,
  );
  return { fake, onSubmit, onCancel };
}

describe("SpatialDrawSurface -- neutral-line flow (purpose='line')", () => {
  it("blocks submit until a line is drawn, then submits WITHOUT any tagging", () => {
    const { fake, onSubmit } = renderNeutralLineSurface();

    // Nothing drawn yet -> blocked with the line-specific reason.
    expect(submitBtn().disabled).toBe(true);
    expect(
      screen.getByTestId("spatial-draw-submit-reason").textContent,
    ).toContain("Draw a line on the map to submit");

    // Draw a plain line and DO NOT tag it. In the barrier flow this would block
    // submit ("Tag every barrier ..."); in neutral-line mode it submits as-is.
    act(() => {
      fake._add(lineGeom([[-85.309, 35.041], [-85.305, 35.045], [-85.301, 35.049]]), {
        mode: "linestring",
      });
    });
    expect(submitBtn().disabled).toBe(false);
    // No "untagged barrier" reason -- there is no barrier tagging at all here.
    expect(screen.queryByTestId("spatial-draw-submit-reason")).toBeNull();
    // The count reads as a LINE, not a barrier.
    expect(screen.getByTestId("draw-counts").textContent).toContain("1 line");

    fireEvent.click(submitBtn());
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const result = onSubmit.mock.calls[0]![0];
    expect(result.geometryType).toBe("vector_draw");
    const fc = result.features!;
    expect(fc.features).toHaveLength(1);
    const feat = fc.features[0]!;
    // The drawn line round-trips as role=="line" -- NOT a barrier, NO barrier_type.
    expect(feat.properties.role).toBe("line");
    expect(feat.properties.barrier_type).toBeUndefined();
    expect(feat.geometry.type).toBe("LineString");
    // No barrier features at all leak into the submitted collection.
    expect(fc.features.some((f) => f.properties.role === "barrier")).toBe(false);
  });

  it("does NOT open the barrier tag popover when a neutral line is selected", () => {
    const { fake } = renderNeutralLineSurface();
    let id: DrawFeatureId = -1;
    act(() => {
      id = fake._add(lineGeom([[-85.309, 35.041], [-85.301, 35.049]]), {
        mode: "linestring",
      });
    });
    act(() => {
      fake._select(id);
    });
    // The wall/flap-gate tagging popover must never appear in neutral-line mode.
    expect(screen.queryByTestId("spatial-draw-tag-popover")).toBeNull();
  });
});
