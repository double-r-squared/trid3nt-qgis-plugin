// GRACE-2 web — Map.tsx feature-click/tap-to-inspect tests (F74b).
//
// Verifies the click/tap-to-inspect handler the agent advertises but which had
// no implementation before this feature:
//   1. A click that queryRenderedFeatures returns a hit for opens the popup
//      with the feature's name / designation / IUCN attributes.
//   2. A TAP (the same MapLibre `click` event fires on touch) with a hit opens
//      the popup → mobile path works.
//   3. A click with NO hit dismisses any open popup.
//   4. The popup is dismissable via its X button.
//   5. queryRenderedFeatures is restricted to the rendered vector layers.
//   6. Hover sets the canvas cursor to "pointer" over a hittable feature.
//   7. The pure property-extraction helpers (humanize / stringify / build).
//
// maplibre-gl is mocked (no WebGL in happy-dom). This mock — unlike the one in
// Map.test.tsx — actually REGISTERS event handlers (on/off) and implements
// queryRenderedFeatures + getCanvas().style so the click/cursor paths run.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, act, screen, fireEvent } from "@testing-library/react";
import {
  MapView,
  buildFeaturePopupData,
  buildHighlightGeoJson,
  computeBboxScreenWidth,
  humanizePropertyKey,
  stringifyPropertyValue,
  FEATURE_HIGHLIGHT_SOURCE_ID,
  FEATURE_HIGHLIGHT_FILL_LAYER_ID,
  FEATURE_HIGHLIGHT_LINE_LAYER_ID,
  FEATURE_HIGHLIGHT_CIRCLE_LAYER_ID,
  LEGEND_MIN_WIDTH_PX,
  type SessionStateSubscriber,
} from "./Map";
import {
  FeaturePopup,
  resolvePopupPlacement,
  resolvePopupScale,
  POPUP_MIN_SCALE,
  POPUP_MAX_SCALE,
} from "./components/FeaturePopup";

// --- MapLibre mock with real event registration + queryRenderedFeatures ---- //

type Listener = (e: unknown) => void;

interface FeatureInspectMapMock {
  addSource: ReturnType<typeof vi.fn>;
  addLayer: ReturnType<typeof vi.fn>;
  removeLayer: ReturnType<typeof vi.fn>;
  removeSource: ReturnType<typeof vi.fn>;
  setPaintProperty: ReturnType<typeof vi.fn>;
  setLayoutProperty: ReturnType<typeof vi.fn>;
  moveLayer: ReturnType<typeof vi.fn>;
  fitBounds: ReturnType<typeof vi.fn>;
  getLayer: ReturnType<typeof vi.fn>;
  getSource: ReturnType<typeof vi.fn>;
  isStyleLoaded: ReturnType<typeof vi.fn>;
  queryRenderedFeatures: ReturnType<typeof vi.fn>;
  getZoom: ReturnType<typeof vi.fn>;
  remove: ReturnType<typeof vi.fn>;
  on: (ev: string, h: Listener) => void;
  off: (ev: string, h: Listener) => void;
  once: ReturnType<typeof vi.fn>;
  project: ReturnType<typeof vi.fn>;
  getCanvas: () => { clientWidth: number; clientHeight: number; style: { cursor: string } };
  getStyle: ReturnType<typeof vi.fn>;
  _emit: (ev: string, e: unknown) => void;
  _canvasStyle: { cursor: string };
  _addedLayers: Set<string>;
  _addedLayerDefs: Map<string, { id: string; filter?: unknown }>;
  _addedSources: Set<string>;
}

let lastMapMock: FeatureInspectMapMock | null = null;

vi.mock("maplibre-gl", () => {
  class MockMap {
    _addedLayers = new Set<string>(["qgis-basemap", "osm-fallback-basemap"]);
    _addedLayerDefs = new Map<string, { id: string; filter?: unknown }>();
    _addedSources = new Set<string>(["qgis-wms", "osm-fallback"]);
    _listeners = new Map<string, Listener[]>();
    _canvasStyle = { cursor: "" };

    addSource = vi.fn((id: string) => {
      this._addedSources.add(id);
    });
    addLayer = vi.fn((def: { id: string; filter?: unknown }) => {
      this._addedLayers.add(def.id);
      // FIX 3 (NATE 2026-06-28): retain the FULL layer def so a test can assert
      // the geometry-type filter on the highlight CIRCLE layer.
      this._addedLayerDefs.set(def.id, def);
    });
    removeLayer = vi.fn((id: string) => this._addedLayers.delete(id));
    removeSource = vi.fn((id: string) => this._addedSources.delete(id));
    setPaintProperty = vi.fn();
    setLayoutProperty = vi.fn();
    moveLayer = vi.fn();
    fitBounds = vi.fn();
    getLayer = vi.fn((id: string) => (this._addedLayers.has(id) ? { id } : null));
    getSource = vi.fn((id: string) =>
      this._addedSources.has(id) ? { type: "geojson", setData: vi.fn() } : null,
    );
    isStyleLoaded = vi.fn().mockReturnValue(true);
    queryRenderedFeatures = vi.fn(() => [] as unknown[]);
    getZoom = vi.fn().mockReturnValue(8);
    remove = vi.fn();
    touchZoomRotate = { disableRotation: vi.fn() };
    keyboard = { disableRotation: vi.fn() };
    addControl = vi.fn();

    on = (ev: string, h: Listener): void => {
      const arr = this._listeners.get(ev) ?? [];
      arr.push(h);
      this._listeners.set(ev, arr);
    };
    off = (ev: string, h: Listener): void => {
      const arr = this._listeners.get(ev);
      if (arr) this._listeners.set(ev, arr.filter((x) => x !== h));
    };
    once = vi.fn();
    _emit = (ev: string, e: unknown): void => {
      (this._listeners.get(ev) ?? []).forEach((h) => h(e));
    };

    project = vi.fn((ll: [number, number]) => ({ x: (ll[0] + 180) * 2, y: (90 - ll[1]) * 2 }));
    getCanvas = (): { clientWidth: number; clientHeight: number; style: { cursor: string } } => ({
      clientWidth: 1024,
      clientHeight: 768,
      style: this._canvasStyle,
    });
    getStyle = vi.fn(() => ({
      layers: Array.from(this._addedLayers).map((id) => ({ id })),
      sources: {},
    }));

    constructor() {
      lastMapMock = this as unknown as FeatureInspectMapMock;
    }
  }

  return {
    default: { Map: MockMap, NavigationControl: class {} },
    Map: MockMap,
    NavigationControl: class {},
  };
});

vi.mock("maplibre-gl/dist/maplibre-gl.css", () => ({}));

// Make a vector layer "rendered" so the click handler's queryableLayerIds()
// includes it: push session-state with a polygon vector layer (inline GeoJSON
// so no fetch is needed) and let the async addVectorLayer register it.
interface WireSessionState {
  loaded_layers?: Array<Record<string, unknown>>;
}
type SessionSubscriber = (p: WireSessionState) => void;

function makeSessionBus() {
  const subs: SessionSubscriber[] = [];
  return {
    push: (p: WireSessionState) => subs.forEach((s) => s(p)),
    subscribe: (cb: SessionSubscriber) => {
      subs.push(cb);
      return () => subs.splice(subs.indexOf(cb), 1);
    },
  };
}

function wdpaInlineLayer(id = "wdpa-big-cypress") {
  return {
    layer_id: id,
    name: id,
    layer_type: "vector",
    uri: "gs://unused-because-inline",
    visible: true,
    opacity: 1,
    style_preset: "wdpa_polygon",
    inline_geojson: {
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          geometry: {
            type: "Polygon",
            coordinates: [[[-81, 26], [-81.1, 26], [-81.1, 26.1], [-81, 26.1], [-81, 26]]],
          },
          properties: {
            name_eng: "Big Cypress National Preserve",
            desig_eng: "National Preserve",
            iucn_cat: "II",
            status_yr: 1974,
          },
        },
      ],
    },
  };
}

async function renderWithRenderedVectorLayer(): Promise<FeatureInspectMapMock> {
  const sessionBus = makeSessionBus();
  render(
    <MapView
      subscribeSessionState={
        sessionBus.subscribe as unknown as (cb: SessionStateSubscriber) => () => void
      }
    />,
  );
  await act(async () => {
    sessionBus.push({ loaded_layers: [wdpaInlineLayer()] });
    // addVectorLayer is async (inline path is sync work but wrapped in a promise).
    await Promise.resolve();
    await Promise.resolve();
  });
  return lastMapMock!;
}

// --- Tests ---------------------------------------------------------------- //

describe("MapView — feature click/tap-to-inspect (F74b)", () => {
  beforeEach(() => {
    lastMapMock = null;
  });

  it("opens a popup with name / designation / IUCN when a click hits a vector feature", async () => {
    const m = await renderWithRenderedVectorLayer();
    expect(m._addedLayers.has("wdpa-big-cypress")).toBe(true);

    // The hit feature MapLibre returns from queryRenderedFeatures.
    m.queryRenderedFeatures.mockReturnValue([
      {
        layer: { id: "wdpa-big-cypress", source: "wdpa-big-cypress" },
        properties: {
          name_eng: "Big Cypress National Preserve",
          desig_eng: "National Preserve",
          iucn_cat: "II",
          status_yr: 1974,
        },
      },
    ]);

    act(() => {
      m._emit("click", { point: { x: 400, y: 300 } });
    });

    // queryRenderedFeatures must be restricted to the rendered vector layer.
    const qrfArgs = m.queryRenderedFeatures.mock.calls[0];
    expect((qrfArgs?.[1] as { layers: string[] }).layers).toContain("wdpa-big-cypress");

    // Popup shows the name as title, designation as subtitle, IUCN as a row.
    expect(screen.getByTestId("grace2-feature-popup")).toBeTruthy();
    expect(screen.getByTestId("feature-popup-title").textContent).toBe(
      "Big Cypress National Preserve",
    );
    expect(screen.getByTestId("feature-popup-subtitle").textContent).toBe(
      "National Preserve",
    );
    const attrs = screen.getByTestId("feature-popup-attributes").textContent ?? "";
    expect(attrs).toContain("IUCN Category");
    expect(attrs).toContain("II");
    // Other non-name/desig/iucn props are humanized + shown.
    expect(attrs).toContain("Status Yr");
    expect(attrs).toContain("1974");
  });

  it("opens the popup on a TAP too (MapLibre fires the same `click` from a tap) — mobile path", async () => {
    const m = await renderWithRenderedVectorLayer();
    m.queryRenderedFeatures.mockReturnValue([
      {
        layer: { id: "wdpa-big-cypress", source: "wdpa-big-cypress" },
        properties: { name_eng: "Tapped Reserve", iucn_cat: "Ia" },
      },
    ]);

    // A tap surfaces as a `click` MapMouseEvent in MapLibre once the tap did
    // not pan — emitting it exercises the exact same handler touch users hit.
    act(() => {
      m._emit("click", { point: { x: 120, y: 600 } });
    });

    expect(screen.getByTestId("feature-popup-title").textContent).toBe("Tapped Reserve");
    expect(screen.getByTestId("feature-popup-attributes").textContent).toContain("Ia");
  });

  it("dismisses the popup when a click hits empty map (no feature)", async () => {
    const m = await renderWithRenderedVectorLayer();
    m.queryRenderedFeatures.mockReturnValue([
      { layer: { id: "wdpa-big-cypress", source: "wdpa-big-cypress" }, properties: { name_eng: "X" } },
    ]);
    act(() => {
      m._emit("click", { point: { x: 400, y: 300 } });
    });
    expect(screen.queryByTestId("grace2-feature-popup")).toBeTruthy();

    // Now a click that hits nothing.
    m.queryRenderedFeatures.mockReturnValue([]);
    act(() => {
      m._emit("click", { point: { x: 10, y: 10 } });
    });
    expect(screen.queryByTestId("grace2-feature-popup")).toBeNull();
  });

  it("dismisses the popup via the X button", async () => {
    const m = await renderWithRenderedVectorLayer();
    m.queryRenderedFeatures.mockReturnValue([
      { layer: { id: "wdpa-big-cypress", source: "wdpa-big-cypress" }, properties: { name_eng: "Closeable" } },
    ]);
    act(() => {
      m._emit("click", { point: { x: 400, y: 300 } });
    });
    expect(screen.queryByTestId("grace2-feature-popup")).toBeTruthy();

    act(() => {
      fireEvent.click(screen.getByTestId("feature-popup-close"));
    });
    expect(screen.queryByTestId("grace2-feature-popup")).toBeNull();
  });

  it("sets the canvas cursor to pointer over a hittable feature and clears it otherwise", async () => {
    const m = await renderWithRenderedVectorLayer();

    m.queryRenderedFeatures.mockReturnValue([
      { layer: { id: "wdpa-big-cypress", source: "wdpa-big-cypress" }, properties: {} },
    ]);
    act(() => {
      m._emit("mousemove", { point: { x: 400, y: 300 } });
    });
    expect(m._canvasStyle.cursor).toBe("pointer");

    m.queryRenderedFeatures.mockReturnValue([]);
    act(() => {
      m._emit("mousemove", { point: { x: 5, y: 5 } });
    });
    expect(m._canvasStyle.cursor).toBe("");
  });

  it("does not query when no vector layers are rendered (raster-only map)", () => {
    render(<MapView />);
    const m = lastMapMock!;
    // No vector layers tracked → click is a no-op (and dismisses nothing).
    act(() => {
      m._emit("click", { point: { x: 400, y: 300 } });
    });
    expect(m.queryRenderedFeatures).not.toHaveBeenCalled();
    expect(screen.queryByTestId("grace2-feature-popup")).toBeNull();
  });
});

// --- FIX 1 (NATE 2026-06-17): generic whole-feature highlight on tap -------- //
//
// Tapping a vector feature must ALSO highlight the ENTIRE tapped geometry,
// generically (polygon / line / point), via a single dedicated highlight source
// + fill/line/circle layers. The highlight source must receive the TAPPED
// feature's geometry, and the highlight must be cleared when the popup closes.

/** Read back the `data` (FeatureCollection) the highlight source was given,
 *  preferring the latest setData() call, else the initial addSource() opts. */
function readHighlightData(m: FeatureInspectMapMock): {
  type: string;
  features: Array<{ geometry: { type: string } }>;
} | null {
  // The highlight source is created via addSource(id, { type, data }); later
  // taps swap via getSource(id).setData(data). Inspect the addSource opts.
  const calls = (m.addSource as unknown as { mock: { calls: unknown[][] } }).mock.calls;
  for (let i = calls.length - 1; i >= 0; i--) {
    const [id, opts] = calls[i] as [string, { data?: unknown } | undefined];
    if (id === FEATURE_HIGHLIGHT_SOURCE_ID && opts && opts.data) {
      return opts.data as ReturnType<typeof readHighlightData>;
    }
  }
  return null;
}

describe("MapView — generic feature highlight on tap (FIX 1)", () => {
  beforeEach(() => {
    lastMapMock = null;
  });

  it("adds a highlight source + fill/line/circle layers and feeds it the tapped POLYGON geometry", async () => {
    const m = await renderWithRenderedVectorLayer();
    m.queryRenderedFeatures.mockReturnValue([
      {
        layer: { id: "wdpa-big-cypress", source: "wdpa-big-cypress" },
        geometry: {
          type: "Polygon",
          coordinates: [[[-81, 26], [-81.1, 26], [-81.1, 26.1], [-81, 26.1], [-81, 26]]],
        },
        properties: { name_eng: "Big Cypress" },
      },
    ]);
    act(() => {
      m._emit("click", { point: { x: 400, y: 300 }, lngLat: { lng: -81.05, lat: 26.05 } });
    });

    // All three highlight paint layers exist (generic across geometries).
    expect(m._addedLayers.has(FEATURE_HIGHLIGHT_FILL_LAYER_ID)).toBe(true);
    expect(m._addedLayers.has(FEATURE_HIGHLIGHT_LINE_LAYER_ID)).toBe(true);
    expect(m._addedLayers.has(FEATURE_HIGHLIGHT_CIRCLE_LAYER_ID)).toBe(true);
    // The highlight source carries exactly the tapped polygon geometry.
    const data = readHighlightData(m);
    expect(data?.features).toHaveLength(1);
    expect(data?.features[0]?.geometry.type).toBe("Polygon");
  });

  it("FIX 3 (NATE 2026-06-28): the highlight CIRCLE layer carries a Point geometry-type filter so NO vertex dots paint around a polygon", async () => {
    const m = await renderWithRenderedVectorLayer();
    m.queryRenderedFeatures.mockReturnValue([
      {
        layer: { id: "wdpa-big-cypress", source: "wdpa-big-cypress" },
        geometry: {
          type: "Polygon",
          coordinates: [[[-81, 26], [-81.1, 26], [-81.1, 26.1], [-81, 26.1], [-81, 26]]],
        },
        properties: { name_eng: "Big Cypress" },
      },
    ]);
    act(() => {
      m._emit("click", { point: { x: 400, y: 300 }, lngLat: { lng: -81.05, lat: 26.05 } });
    });
    // The circle layer was added WITH the Point-only filter. Without it, MapLibre
    // would paint the ring at every polygon vertex (the stray dots bug). fill +
    // line carry NO such filter (the polygon wash + outline must still paint).
    const circleDef = m._addedLayerDefs.get(FEATURE_HIGHLIGHT_CIRCLE_LAYER_ID);
    expect(circleDef?.filter).toEqual(["==", ["geometry-type"], "Point"]);
    const fillDef = m._addedLayerDefs.get(FEATURE_HIGHLIGHT_FILL_LAYER_ID);
    const lineDef = m._addedLayerDefs.get(FEATURE_HIGHLIGHT_LINE_LAYER_ID);
    expect(fillDef?.filter).toBeUndefined();
    expect(lineDef?.filter).toBeUndefined();
  });

  it("feeds the highlight source a tapped LINE geometry (roads / rivers)", async () => {
    const m = await renderWithRenderedVectorLayer();
    m.queryRenderedFeatures.mockReturnValue([
      {
        layer: { id: "wdpa-big-cypress", source: "wdpa-big-cypress" },
        geometry: { type: "LineString", coordinates: [[-81, 26], [-81.1, 26.1]] },
        properties: { name: "Tamiami Trail", highway: "primary" },
      },
    ]);
    act(() => {
      m._emit("click", { point: { x: 400, y: 300 }, lngLat: { lng: -81.05, lat: 26.05 } });
    });
    const data = readHighlightData(m);
    expect(data?.features[0]?.geometry.type).toBe("LineString");
    // The line highlight layer (used for line + polygon boundary) is present.
    expect(m._addedLayers.has(FEATURE_HIGHLIGHT_LINE_LAYER_ID)).toBe(true);
  });

  it("feeds the highlight source a tapped POINT geometry (gauges / occurrences)", async () => {
    const m = await renderWithRenderedVectorLayer();
    m.queryRenderedFeatures.mockReturnValue([
      {
        layer: { id: "wdpa-big-cypress", source: "wdpa-big-cypress" },
        geometry: { type: "Point", coordinates: [-81.05, 26.05] },
        properties: { name: "Gauge 02290769" },
      },
    ]);
    act(() => {
      m._emit("click", { point: { x: 400, y: 300 }, lngLat: { lng: -81.05, lat: 26.05 } });
    });
    const data = readHighlightData(m);
    expect(data?.features[0]?.geometry.type).toBe("Point");
    // The circle highlight layer (enlarged ring) is present.
    expect(m._addedLayers.has(FEATURE_HIGHLIGHT_CIRCLE_LAYER_ID)).toBe(true);
    // FIX 3 (NATE 2026-06-28): a genuine POINT tap satisfies the Point-only
    // filter, so the ring STILL paints (the filter scopes dots away from
    // polygon vertices without suppressing real point highlights).
    const circleDef = m._addedLayerDefs.get(FEATURE_HIGHLIGHT_CIRCLE_LAYER_ID);
    expect(circleDef?.filter).toEqual(["==", ["geometry-type"], "Point"]);
  });

  it("clears the highlight (removes layers + source) when the popup is closed via X", async () => {
    const m = await renderWithRenderedVectorLayer();
    m.queryRenderedFeatures.mockReturnValue([
      {
        layer: { id: "wdpa-big-cypress", source: "wdpa-big-cypress" },
        geometry: { type: "Point", coordinates: [-81, 26] },
        properties: { name: "X" },
      },
    ]);
    act(() => {
      m._emit("click", { point: { x: 400, y: 300 }, lngLat: { lng: -81, lat: 26 } });
    });
    expect(m._addedSources.has(FEATURE_HIGHLIGHT_SOURCE_ID)).toBe(true);

    act(() => {
      fireEvent.click(screen.getByTestId("feature-popup-close"));
    });
    // Highlight torn down: all three layers + the source removed.
    expect(m._addedLayers.has(FEATURE_HIGHLIGHT_FILL_LAYER_ID)).toBe(false);
    expect(m._addedLayers.has(FEATURE_HIGHLIGHT_LINE_LAYER_ID)).toBe(false);
    expect(m._addedLayers.has(FEATURE_HIGHLIGHT_CIRCLE_LAYER_ID)).toBe(false);
    expect(m._addedSources.has(FEATURE_HIGHLIGHT_SOURCE_ID)).toBe(false);
  });
});

// --- FIX 2 (NATE 2026-06-17): popup PINNED TO THE MAP (re-projected on move) - //
//
// The popup carries the tapped feature's lng/lat and is re-projected to a screen
// point on every map move/zoom, so it stays glued to the feature's MAP location.

describe("MapView — popup pinned to the map (FIX 2)", () => {
  // Deferred-flush rAF stub: store callbacks, return an id, flush on demand.
  // This matches REAL rAF semantics (the callback runs AFTER schedule() returns
  // and assigns its rafId) — a synchronous stub would run the callback before
  // the rafId assignment and break the once-per-frame coalescing guard.
  let rafQueue: FrameRequestCallback[] = [];
  let rafSpy: ReturnType<typeof vi.spyOn> | null = null;
  const flushRaf = (): void => {
    const q = rafQueue;
    rafQueue = [];
    q.forEach((cb) => cb(0));
  };

  beforeEach(() => {
    lastMapMock = null;
    rafQueue = [];
    rafSpy = vi
      .spyOn(globalThis, "requestAnimationFrame")
      .mockImplementation((cb: FrameRequestCallback) => {
        rafQueue.push(cb);
        return rafQueue.length as unknown as number;
      });
  });

  it("re-projects the popup from its lngLat when the map pans (move)", async () => {
    const m = await renderWithRenderedVectorLayer();
    m.queryRenderedFeatures.mockReturnValue([
      {
        layer: { id: "wdpa-big-cypress", source: "wdpa-big-cypress" },
        geometry: { type: "Point", coordinates: [-81, 26] },
        properties: { name_eng: "Pinned Preserve" },
      },
    ]);
    act(() => {
      m._emit("click", { point: { x: 400, y: 300 }, lngLat: { lng: -81, lat: 26 } });
    });
    // Flush the initial projection scheduled when the popup-pin effect armed.
    act(() => {
      flushRaf();
    });
    expect(screen.getByTestId("grace2-feature-popup")).toBeTruthy();

    // Now PAN the map. The popup-pin effect must re-project the popup's lngLat.
    m.project.mockClear();
    act(() => {
      m._emit("move", {});
      flushRaf();
    });
    // project() was called with the popup's geographic anchor → the screen point
    // is derived FROM the lngLat (FIX 2), not frozen at the tap pixel.
    const projectedWith = m.project.mock.calls.map((c) => c[0]);
    expect(projectedWith).toContainEqual([-81, 26]);

    rafSpy?.mockRestore();
  });

  it("does not re-project (no lngLat) for a hit without a geographic anchor", async () => {
    const m = await renderWithRenderedVectorLayer();
    m.queryRenderedFeatures.mockReturnValue([
      {
        layer: { id: "wdpa-big-cypress", source: "wdpa-big-cypress" },
        geometry: { type: "Point", coordinates: [-81, 26] },
        properties: { name_eng: "No Anchor" },
      },
    ]);
    // Click WITHOUT lngLat → popup is screen-anchored (older-fixture path).
    act(() => {
      m._emit("click", { point: { x: 400, y: 300 } });
    });
    expect(screen.getByTestId("grace2-feature-popup")).toBeTruthy();
    m.project.mockClear();
    act(() => {
      m._emit("move", {});
      flushRaf();
    });
    // No geographic anchor → the popup-pin effect is inert (no re-projection).
    expect(m.project).not.toHaveBeenCalled();

    rafSpy?.mockRestore();
  });
});

// --- FIX 1 pure helper: buildHighlightGeoJson ------------------------------ //

describe("buildHighlightGeoJson (FIX 1)", () => {
  it("wraps a polygon geometry in a single-feature FeatureCollection (cloned)", () => {
    const geom = {
      type: "Polygon" as const,
      coordinates: [[[-81, 26], [-81.1, 26], [-81.1, 26.1], [-81, 26]]],
    };
    const fc = buildHighlightGeoJson(geom);
    expect(fc.type).toBe("FeatureCollection");
    expect(fc.features).toHaveLength(1);
    expect(fc.features[0]?.geometry).toEqual(geom);
    // Cloned, not the same reference (so a later setData can't mutate it).
    expect(fc.features[0]?.geometry).not.toBe(geom);
  });

  it("wraps line + point geometries too (generic)", () => {
    const line = buildHighlightGeoJson({
      type: "LineString",
      coordinates: [[0, 0], [1, 1]],
    });
    expect(line.features[0]?.geometry.type).toBe("LineString");
    const pt = buildHighlightGeoJson({ type: "Point", coordinates: [0, 0] });
    expect(pt.features[0]?.geometry.type).toBe("Point");
  });

  it("returns an EMPTY FeatureCollection for null/undefined geometry", () => {
    expect(buildHighlightGeoJson(null).features).toHaveLength(0);
    expect(buildHighlightGeoJson(undefined).features).toHaveLength(0);
  });
});

// --- FIX 3 pure helper: resolvePopupScale ---------------------------------- //

describe("resolvePopupScale (FIX 3 — popup scales with zoom, clamped)", () => {
  it("is 1 at the reference zoom (no change)", () => {
    expect(resolvePopupScale(12, 12)).toBe(1);
  });

  it("grows when zoomed IN past the reference (up to the max clamp)", () => {
    // +1 zoom level → 2x, but clamped to POPUP_MAX_SCALE (1.5).
    expect(resolvePopupScale(12, 13)).toBe(POPUP_MAX_SCALE);
    // A small zoom-in stays below the clamp: 2^0.5 ≈ 1.414.
    expect(resolvePopupScale(12, 12.5)).toBeCloseTo(Math.SQRT2, 3);
    expect(resolvePopupScale(12, 12.5)).toBeLessThanOrEqual(POPUP_MAX_SCALE);
  });

  it("shrinks when zoomed OUT below the reference (down to the min clamp)", () => {
    // -2 zoom levels → 0.25, but clamped to POPUP_MIN_SCALE (0.5).
    expect(resolvePopupScale(12, 10)).toBe(POPUP_MIN_SCALE);
    // A small zoom-out stays above the floor: 2^-0.5 ≈ 0.707.
    expect(resolvePopupScale(12, 11.5)).toBeCloseTo(1 / Math.SQRT2, 3);
    expect(resolvePopupScale(12, 11.5)).toBeGreaterThanOrEqual(POPUP_MIN_SCALE);
  });

  it("never exceeds the clamp range for extreme zoom deltas", () => {
    expect(resolvePopupScale(0, 22)).toBe(POPUP_MAX_SCALE);
    expect(resolvePopupScale(22, 0)).toBe(POPUP_MIN_SCALE);
  });

  it("returns 1 when either zoom is missing / non-finite", () => {
    expect(resolvePopupScale(undefined, 12)).toBe(1);
    expect(resolvePopupScale(12, undefined)).toBe(1);
    expect(resolvePopupScale(NaN, 12)).toBe(1);
  });
});

// --- FIX 4 pure helper: computeBboxScreenWidth ----------------------------- //
//
// The legend colorbar width is sized to the AOI bbox's on-screen east-west
// extent (projected), clamped to [LEGEND_MIN_WIDTH_PX, viewport - margins].

interface WidthProbeMap {
  project: (ll: [number, number]) => { x: number; y: number };
  getCanvas: () => { clientWidth: number; clientHeight: number };
}

function makeWidthProbeMap(opts: {
  // projected x per lon (linear) so we control the on-screen east-west span.
  pxPerLon: number;
  canvasWidth: number;
}): WidthProbeMap {
  return {
    project: (ll) => ({ x: ll[0] * opts.pxPerLon, y: 0 }),
    getCanvas: () => ({ clientWidth: opts.canvasWidth, clientHeight: 600 }),
  };
}

describe("computeBboxScreenWidth (FIX 4 — legend width sized to bbox on-screen)", () => {
  const bbox: [number, number, number, number] = [0, 0, 10, 10];

  it("derives the width from the projected east-west extent of the bbox", () => {
    // 10 lon * 30 px/lon = 300 px on-screen extent; canvas is wide enough.
    const m = makeWidthProbeMap({ pxPerLon: 30, canvasWidth: 1200 });
    const w = computeBboxScreenWidth(m as unknown as Parameters<typeof computeBboxScreenWidth>[0], bbox);
    expect(w).toBe(300);
  });

  it("clamps UP to the minimum when the bbox is small on screen (zoomed out)", () => {
    // 10 lon * 5 px/lon = 50 px raw → below the 160px floor → clamped up.
    const m = makeWidthProbeMap({ pxPerLon: 5, canvasWidth: 1200 });
    const w = computeBboxScreenWidth(m as unknown as Parameters<typeof computeBboxScreenWidth>[0], bbox);
    expect(w).toBe(LEGEND_MIN_WIDTH_PX);
  });

  it("clamps DOWN to viewport-minus-margins when the bbox overflows (zoomed in)", () => {
    // 10 lon * 100 px/lon = 1000 px raw, but canvas is 500 wide → max = 500-48.
    const m = makeWidthProbeMap({ pxPerLon: 100, canvasWidth: 500 });
    const w = computeBboxScreenWidth(m as unknown as Parameters<typeof computeBboxScreenWidth>[0], bbox);
    expect(w).toBe(500 - 24 * 2);
  });

  it("returns null when projection throws (off-screen / no map)", () => {
    const throwingMap = {
      project: () => {
        throw new Error("no gl");
      },
      getCanvas: () => ({ clientWidth: 1200, clientHeight: 600 }),
    };
    expect(
      computeBboxScreenWidth(
        throwingMap as unknown as Parameters<typeof computeBboxScreenWidth>[0],
        bbox,
      ),
    ).toBeNull();
  });
});

// --- pure helper unit tests ---------------------------------------------- //

describe("feature-inspect pure helpers", () => {
  it("humanizePropertyKey turns snake/camel keys into Title Case", () => {
    expect(humanizePropertyKey("name_eng")).toBe("Name Eng");
    expect(humanizePropertyKey("iucn_cat")).toBe("Iucn Cat");
    expect(humanizePropertyKey("scientificName")).toBe("Scientific Name");
    expect(humanizePropertyKey("status-yr")).toBe("Status Yr");
  });

  it("stringifyPropertyValue handles strings, numbers, bools, and drops empties", () => {
    expect(stringifyPropertyValue("National Preserve")).toBe("National Preserve");
    expect(stringifyPropertyValue("  ")).toBeNull();
    expect(stringifyPropertyValue(1974)).toBe("1974");
    expect(stringifyPropertyValue(0.123456)).toBe("0.123");
    expect(stringifyPropertyValue(true)).toBe("Yes");
    expect(stringifyPropertyValue(false)).toBe("No");
    expect(stringifyPropertyValue(null)).toBeNull();
    expect(stringifyPropertyValue(undefined)).toBeNull();
    expect(stringifyPropertyValue(NaN)).toBeNull();
  });

  it("buildFeaturePopupData picks name/designation/IUCN and de-noises the rest", () => {
    const data = buildFeaturePopupData(
      {
        name_eng: "Big Cypress National Preserve",
        desig_eng: "National Preserve",
        iucn_cat: "II",
        status: "Designated",
        objectid: 42, // hidden noise key
      },
      { x: 100, y: 100 },
      { layerName: "wdpa-big-cypress" },
    );
    expect(data.title).toBe("Big Cypress National Preserve");
    expect(data.subtitle).toBe("National Preserve");
    // IUCN leads the attribute list.
    expect(data.attributes[0]).toEqual({ label: "IUCN Category", value: "II" });
    const labels = data.attributes.map((a) => a.label);
    expect(labels).toContain("Status");
    // objectid is a hidden noise key — not shown.
    expect(labels).not.toContain("Objectid");
    // name/desig/iucn are not duplicated into the attribute list.
    expect(labels).not.toContain("Name Eng");
    expect(labels).not.toContain("Desig Eng");
  });

  it("buildFeaturePopupData falls back to geometry-kind label when no name is present", () => {
    const data = buildFeaturePopupData(
      { foo: "bar" },
      { x: 1, y: 2 },
      { geomKindLabel: "Polygon" },
    );
    expect(data.title).toBe("Polygon");
    expect(data.attributes.map((a) => a.label)).toContain("Foo");
  });

  it("buildFeaturePopupData gracefully handles null/empty properties", () => {
    const data = buildFeaturePopupData(null, { x: 0, y: 0 }, { layerName: "layer-x" });
    expect(data.title).toBe("layer-x");
    expect(data.attributes).toEqual([]);
  });
});

// --- FIX 3 (F86): popup anchored at the tap/click point on BOTH surfaces --- //
//
// "the popup should be where I tapped." resolvePopupPlacement must anchor near
// the click point on desktop AND mobile (the prior mobile behaviour pinned to
// the canvas bottom-center, detaching it from the tap), clamped into the canvas
// so it can never run off an edge.

describe("resolvePopupPlacement — anchored at the tap point (FIX 3 / F86)", () => {
  const canvas = { width: 1000, height: 800 };

  it("desktop: anchors near the click point (upper-right offset)", () => {
    const { left, top } = resolvePopupPlacement({ x: 400, y: 300 }, canvas, false);
    // upper-right of the point: left ~= x + offset, top ~= y - offset.
    expect(left).toBeGreaterThan(400);
    expect(left).toBeLessThan(500);
    expect(top).toBeLessThan(300);
    expect(top).toBeGreaterThan(250);
  });

  it("mobile: ALSO anchors near the tap point — NOT pinned bottom-center", () => {
    const { left, top, width } = resolvePopupPlacement({ x: 200, y: 250 }, canvas, true);
    // left tracks the tap x (was (1000-280)/2 = 360 under the old bottom-center
    // rule); top tracks the tap y (was h - EST - 96 = 484 under the old rule).
    expect(left).toBeGreaterThan(200);
    expect(left).toBeLessThan(280);
    expect(top).toBeLessThan(250);
    // mobile card is the wider touch width.
    expect(width).toBe(280);
  });

  it("clamps a near-edge tap fully into the canvas (both surfaces)", () => {
    // Tap near the bottom-right corner: the card must not run off either edge.
    const m = resolvePopupPlacement({ x: 990, y: 790 }, canvas, true);
    expect(m.left).toBeGreaterThanOrEqual(0);
    expect(m.left + m.width).toBeLessThanOrEqual(canvas.width);
    expect(m.top).toBeGreaterThanOrEqual(0);

    const d = resolvePopupPlacement({ x: 990, y: 790 }, canvas, false);
    expect(d.left).toBeGreaterThanOrEqual(0);
    expect(d.left + d.width).toBeLessThanOrEqual(canvas.width);
    expect(d.top).toBeGreaterThanOrEqual(0);
  });
});

// --- FIX 3 (NATE 2026-06-17): popup SCALES WITH ZOOM (component render) ----- //

describe("FeaturePopup — scales with zoom (FIX 3)", () => {
  const canvas = { width: 1000, height: 800 };
  const baseData = {
    title: "Scaled",
    attributes: [],
    point: { x: 400, y: 300 },
    refZoom: 12,
  };

  it("applies a scale() transform + transform-origin at the anchor when zoomed", () => {
    render(
      <FeaturePopup
        data={{ ...baseData }}
        canvasSize={canvas}
        isMobile={false}
        currentZoom={11.5} // zoomed out a bit → 2^-0.5 ≈ 0.707
        onClose={() => {}}
      />,
    );
    const el = screen.getByTestId("grace2-feature-popup");
    expect(el.getAttribute("data-popup-scale")).toBe(String(1 / Math.SQRT2));
    expect(el.style.transform).toContain("scale(");
    // transform-origin is at the anchor point (POINT_OFFSET = 14px) so it grows
    // around the feature, not the card corner.
    expect(el.style.transformOrigin).toBe("14px 14px");
  });

  it("clamps the scale to the [0.5, 1.5] range", () => {
    const { rerender } = render(
      <FeaturePopup data={{ ...baseData }} canvasSize={canvas} isMobile={false} currentZoom={20} onClose={() => {}} />,
    );
    expect(screen.getByTestId("grace2-feature-popup").getAttribute("data-popup-scale")).toBe("1.5");
    rerender(
      <FeaturePopup data={{ ...baseData }} canvasSize={canvas} isMobile={false} currentZoom={0} onClose={() => {}} />,
    );
    expect(screen.getByTestId("grace2-feature-popup").getAttribute("data-popup-scale")).toBe("0.5");
  });

  it("does not scale (scale 1, no transform) when refZoom / currentZoom is absent", () => {
    render(
      <FeaturePopup
        data={{ title: "Unscaled", attributes: [], point: { x: 10, y: 10 } }}
        canvasSize={canvas}
        isMobile={false}
        onClose={() => {}}
      />,
    );
    const el = screen.getByTestId("grace2-feature-popup");
    expect(el.getAttribute("data-popup-scale")).toBe("1");
    // scale 1 → no transform applied.
    expect(el.style.transform).toBe("");
  });
});
