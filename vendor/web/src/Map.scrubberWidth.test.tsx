// GRACE-2 web — scrubber-width projection tests (NATE 2026-06-20).
//
// The time scrubber's WIDTH equals the AOI bbox's ON-SCREEN pixel width: the bbox
// west/east corners are projected to screen pixels via the MapLibre map instance
// (map.project), width_px = xEast - xWest. Map.tsx projects the FULL AOI rect in
// computeBboxScreenRect (min/max over all four projected corners) and threads it
// up to App via onAoiScreenRectChange; App passes it to the scrubber as aoiRect.
// The scrubber then sets its width to (right - left) of that rect (clamped),
// recomputed on every map move/zoom/render.
//
// These tests pin the projection seam directly (computeBboxScreenRect width ==
// projected deltaX) and the recompute-on-move wiring (firing a map 'move' event
// re-projects + re-reports the rect with the NEW projection).

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";

// --- Event-firing maplibre mock ------------------------------------------- //
// Unlike Map.test.tsx's no-op `on`, this mock registers + dispatches handlers so
// we can fire 'move' and assert the recompute hook re-projects. `project` is a
// per-instance, swappable stub so a test can change the projection between moves
// and prove the reported rect tracks the NEW corners.

interface ScrubMapMock {
  on: ReturnType<typeof vi.fn>;
  off: ReturnType<typeof vi.fn>;
  once: ReturnType<typeof vi.fn>;
  project: (lngLat: [number, number]) => { x: number; y: number };
  fire: (event: string) => void;
}

let lastScrubMap: ScrubMapMock | null = null;

// Captured rAF callbacks (the recompute hook schedules via rAF) — flushed
// manually so the projection runs deterministically AFTER schedule() returns.
const rafQueue: FrameRequestCallback[] = [];
function flushRaf(): void {
  // Drain in FIFO order; a recompute may re-schedule, but it won't re-queue
  // within the same flush because the guard is only cleared as each runs.
  const pending = rafQueue.splice(0, rafQueue.length);
  pending.forEach((cb) => cb(0));
}

vi.mock("maplibre-gl", () => {
  class MockNavigationControl {}

  class MockMap {
    _addedLayers = new Set<string>(["qgis-basemap", "osm-fallback-basemap"]);
    _addedSources = new Set<string>(["qgis-wms", "osm-fallback"]);
    _sourceSetData = new Map<string, ReturnType<typeof vi.fn>>();
    _handlers: Record<string, Array<() => void>> = {};

    addSource = vi.fn((id: string) => {
      this._addedSources.add(id);
      this._sourceSetData.set(id, vi.fn());
    });
    addLayer = vi.fn((def: { id: string }) => {
      this._addedLayers.add(def.id);
    });
    removeLayer = vi.fn((id: string) => this._addedLayers.delete(id));
    removeSource = vi.fn((id: string) => this._addedSources.delete(id));
    setPaintProperty = vi.fn();
    setLayoutProperty = vi.fn();
    moveLayer = vi.fn();
    fitBounds = vi.fn();
    addControl = vi.fn();
    touchZoomRotate = { disableRotation: vi.fn() };
    keyboard = { disableRotation: vi.fn() };
    isStyleLoaded = vi.fn().mockReturnValue(true);
    getLayer = vi.fn((id: string) => (this._addedLayers.has(id) ? { id } : null));
    getSource = vi.fn((id: string) =>
      this._addedSources.has(id)
        ? { type: "geojson", setData: this._sourceSetData.get(id) ?? vi.fn() }
        : null,
    );
    remove = vi.fn();

    // Register/dispatch so the recompute hook's m.on("move", ...) is live.
    on = vi.fn((event: string, cb: () => void) => {
      (this._handlers[event] ??= []).push(cb);
    });
    off = vi.fn((event: string, cb: () => void) => {
      this._handlers[event] = (this._handlers[event] ?? []).filter((h) => h !== cb);
    });
    once = vi.fn();
    fire = (event: string) => {
      (this._handlers[event] ?? []).slice().forEach((cb) => cb());
    };

    // Swappable projection. Default: a monotonic, on-screen mapping. Tests
    // overwrite `project` between moves to simulate a pan/zoom that changes the
    // bbox's on-screen extent.
    project = (lngLat: [number, number]) => ({
      x: (lngLat[0] + 180) * 2,
      y: (90 - lngLat[1]) * 2,
    });
    getCanvas = vi.fn(() => ({ clientWidth: 1024, clientHeight: 768 }));
    getStyle = vi.fn(() => ({
      layers: Array.from(this._addedLayers).map((id) => ({ id })),
      sources: Object.fromEntries(
        Array.from(this._addedSources).map((id) => [id, { type: "raster" }]),
      ),
    }));

    constructor() {
      lastScrubMap = this as unknown as ScrubMapMock;
    }
  }

  return {
    default: { Map: MockMap, NavigationControl: MockNavigationControl },
    Map: MockMap,
    NavigationControl: MockNavigationControl,
  };
});

vi.mock("maplibre-gl/dist/maplibre-gl.css", () => ({}));

import {
  MapView,
  computeBboxScreenRect,
  type MapCommandSubscribeFunc,
  type SessionStateSubscriber,
} from "./Map";
import { LayerCache, setLayerCache } from "./lib/layer_cache";

const noopBackend = {
  async load() {
    return {};
  },
  async save() {
    /* no-op */
  },
};

beforeEach(() => {
  lastScrubMap = null;
  setLayerCache(new LayerCache({ backend: noopBackend }));
  // The recompute hook is rAF-throttled: schedule() does `rafId = rAF(recompute)`
  // (guarded by `rafId != null`), and recompute() sets `rafId = null` on entry.
  // A SYNCHRONOUS rAF stub would mis-order (recompute clears rafId, THEN schedule
  // re-assigns the non-null id, permanently blocking the next schedule). So we
  // QUEUE callbacks and flush them via flushRaf() AFTER schedule() has finished
  // assigning rafId — matching the real async frame timing.
  rafQueue.length = 0;
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    rafQueue.push(cb);
    return rafQueue.length;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// --- Projection seam: width == projected deltaX --------------------------- //

describe("computeBboxScreenRect — rect width == projected on-screen deltaX", () => {
  it("projects bbox W/E corners and the rect width is xEast - xWest", () => {
    // A map whose project() places the bbox at KNOWN screen corners:
    //   west lon -> x = 100, east lon -> x = 640  => width = 540px.
    const m = {
      project: vi.fn((lngLat: [number, number]) => {
        const lon = lngLat[0];
        const lat = lngLat[1];
        const x = lon === -80 ? 100 : 640; // west=-80 -> 100, east=-79 -> 640
        const y = lat === 25 ? 500 : 200; // south=25 -> 500, north=26 -> 200
        return { x, y };
      }),
      getCanvas: () => ({ clientWidth: 1024, clientHeight: 768 }),
    };
    // bbox = [minLon, minLat, maxLon, maxLat]
    const rect = computeBboxScreenRect(m as never, [-80, 25, -79, 26]);
    expect(rect).not.toBeNull();
    // EAST-WEST on-screen extent: xEast(640) - xWest(100) = 540.
    expect(rect!.right - rect!.left).toBe(540);
    // (The scrubber consumes exactly this width.)
    expect(rect!.left).toBe(100);
    expect(rect!.right).toBe(640);
  });

  it("returns null for an off-screen bbox (center outside the canvas)", () => {
    const m = {
      project: vi.fn(() => ({ x: -5000, y: -5000 })),
      getCanvas: () => ({ clientWidth: 1024, clientHeight: 768 }),
    };
    expect(computeBboxScreenRect(m as never, [-80, 25, -79, 26])).toBeNull();
  });
});

// --- Recompute fires on map move ------------------------------------------ //

function makeMapCmdBus() {
  const subs: Array<(p: { command: string; args?: unknown }) => void> = [];
  const push = (p: { command: string; args?: unknown }) => subs.forEach((s) => s(p));
  const subscribe = (cb: (p: { command: string; args?: unknown }) => void) => {
    subs.push(cb);
    return () => subs.splice(subs.indexOf(cb), 1);
  };
  return { push, subscribe };
}

describe("MapView — AOI rect recomputes + re-reports on map 'move'", () => {
  it("fires onAoiScreenRectChange with a NEW rect width after the map moves", () => {
    const cmdBus = makeMapCmdBus();
    const reported: Array<{ left: number; right: number } | null> = [];

    render(
      <MapView
        subscribeMapCommand={cmdBus.subscribe as unknown as MapCommandSubscribeFunc}
        subscribeSessionState={(() => () => {}) as unknown as (
          cb: SessionStateSubscriber,
        ) => () => void}
        onAoiScreenRectChange={(rect) =>
          reported.push(rect ? { left: rect.left, right: rect.right } : null)
        }
      />,
    );

    const m = lastScrubMap!;
    expect(m).not.toBeNull();

    // Push a zoom-to command to establish an AOI bbox. setAoiBbox re-runs the
    // recompute effect, which schedules a recompute via rAF. The effect (and its
    // schedule) commits when act() flushes, so flush rAF in a FOLLOWING act().
    act(() => {
      cmdBus.push({ command: "zoom-to", args: { bbox: [-80, 25, -79, 26] } });
    });
    act(() => {
      flushRaf(); // run the recompute the (now-committed) effect scheduled
    });

    // With the default monotonic projection: west lon -80 -> x=(100)*2=200;
    // east lon -79 -> x=(101)*2=202 => initial width 2px (clamps happen in the
    // scrubber, not here). Capture the initial reported rect.
    const initial = reported[reported.length - 1];
    expect(initial).toBeTruthy();

    // Now simulate a pan/zoom that widens the bbox on screen: swap the
    // projection so the corners land far apart, then fire 'move'.
    m.project = (lngLat: [number, number]) => {
      const lon = lngLat[0];
      // west=-80 -> x=150, east=-79 -> x=950 => width 800px; keep y on-screen.
      const x = lon === -80 ? 150 : 950;
      return { x, y: lngLat[1] === 25 ? 600 : 100 };
    };
    act(() => {
      m.fire("move"); // recompute hook re-projects on every camera 'move'
    });
    act(() => {
      flushRaf(); // run the recompute the move scheduled
    });

    const after = reported[reported.length - 1];
    expect(after).toBeTruthy();
    // The recompute ran on 'move' and re-reported the NEW on-screen width.
    expect(after!.right - after!.left).toBe(800);
    // And it genuinely CHANGED versus the pre-move width (recompute fired).
    expect(after!.right - after!.left).not.toBe(initial!.right - initial!.left);
  });
});
