// GRACE-2 web - Map.tsx <-> deck.gl interleaved-overlay INTEGRATION test
// (deck.gl SPIKE, #169).
//
// PROVES THE COEXISTENCE (the whole point of the spike):
//   - a HEAVY/footprint inline-GeoJSON vector is ROUTED to the deck.gl
//     MapboxOverlay (overlay.setProps gets a layer with that id) and does NOT
//     create a MapLibre source (it stays OFF the MapLibre vector path)
//   - a LIGHT inline-GeoJSON vector is NOT deck-routed (no deck layer for it; it
//     stays on the existing MapLibre path)
//   - a deck-routed layer is TRACKED so an authoritative replace that omits it
//     drops it from the overlay (removal path)
//
// happy-dom has no WebGL, so we MOCK @deck.gl/mapbox's MapboxOverlay with a spy
// that records setProps({layers}) - we assert on the layer IDS routed, not on GL
// rendering. maplibre-gl is mocked with a minimal map whose `once("load")` we
// drive synchronously (the overlay is constructed there).

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act, cleanup } from "@testing-library/react";

// --- deck.gl MapboxOverlay spy ------------------------------------------- //
// Records every setProps call so a test can read back the deck layer ids.
const overlaySetPropsCalls: Array<{ layers: Array<{ id: string }> }> = [];
let overlayInstances = 0;

vi.mock("@deck.gl/mapbox", () => {
  class MockMapboxOverlay {
    onAdd = vi.fn(() => document.createElement("div"));
    onRemove = vi.fn();
    setProps = vi.fn((props: { layers?: Array<{ id: string }> }) => {
      overlaySetPropsCalls.push({ layers: props.layers ?? [] });
    });
    constructor(_opts: unknown) {
      overlayInstances += 1;
    }
  }
  return { MapboxOverlay: MockMapboxOverlay };
});

// --- minimal maplibre-gl mock -------------------------------------------- //
// The mock map instance is published onto globalThis so the test body can read
// it back (vi.mock factories are hoisted above module-level `let`s, so we can't
// close over a normal variable - globalThis is the supported escape hatch).
interface MockMapShape {
  _layers: Set<string>;
  _sources: Set<string>;
  addControl: ReturnType<typeof vi.fn>;
  fireOnce(evt: string): void;
}
function getLastMap(): MockMapShape {
  return (globalThis as unknown as { __deckTestMap: MockMapShape }).__deckTestMap;
}

vi.mock("maplibre-gl", () => {
  interface OnceHandlers {
    [evt: string]: Array<() => void>;
  }
  class MockMap {
    _layers = new Set<string>(["qgis-basemap"]);
    _sources = new Set<string>(["qgis-wms"]);
    _once: OnceHandlers = {};
    _constructorOptions: Record<string, unknown>;
    constructor(opts: Record<string, unknown>) {
      this._constructorOptions = opts;
      (globalThis as unknown as { __deckTestMap: unknown }).__deckTestMap = this;
    }
    addSource = vi.fn((id: string) => this._sources.add(id));
    addLayer = vi.fn((def: { id: string }) => this._layers.add(def.id));
    removeLayer = vi.fn((id: string) => this._layers.delete(id));
    removeSource = vi.fn((id: string) => this._sources.delete(id));
    moveLayer = vi.fn();
    setPaintProperty = vi.fn();
    setLayoutProperty = vi.fn();
    fitBounds = vi.fn();
    addControl = vi.fn();
    removeControl = vi.fn();
    triggerRepaint = vi.fn();
    isStyleLoaded = vi.fn().mockReturnValue(true);
    isSourceLoaded = vi.fn().mockReturnValue(true);
    getLayer = vi.fn((id: string) => (this._layers.has(id) ? { id } : null));
    getSource = vi.fn((id: string) =>
      this._sources.has(id) ? { type: "geojson", setData: vi.fn() } : null,
    );
    getZoom = vi.fn(() => 10);
    getCanvas = vi.fn(() => ({
      style: { cursor: "" },
      clientWidth: 800,
      clientHeight: 600,
    }));
    // applyTheme + the camera/AOI effects probe these; provide benign shapes so a
    // mount does not throw (a throw cascades into React's "already working").
    getStyle = vi.fn(() => ({ layers: [{ id: "qgis-basemap" }] }));
    getPitch = vi.fn(() => 0);
    getBounds = vi.fn(() => ({
      getWest: () => -1,
      getSouth: () => -1,
      getEast: () => 1,
      getNorth: () => 1,
    }));
    getCenter = vi.fn(() => ({ lng: 0, lat: 0 }));
    flyTo = vi.fn();
    easeTo = vi.fn();
    setTerrain = vi.fn();
    setMaxPitch = vi.fn();
    setProjection = vi.fn();
    dragRotate = { enable: vi.fn(), disable: vi.fn() };
    touchPitch = { enable: vi.fn(), disable: vi.fn() };
    queryRenderedFeatures = vi.fn(() => []);
    setFeatureState = vi.fn();
    remove = vi.fn();
    on = vi.fn();
    off = vi.fn();
    once = vi.fn((evt: string, cb: () => void) => {
      (this._once[evt] ??= []).push(cb);
      return this;
    });
    fireOnce(evt: string): void {
      const hs = this._once[evt] ?? [];
      this._once[evt] = [];
      hs.forEach((h) => h());
    }
    touchZoomRotate = { disableRotation: vi.fn(), enableRotation: vi.fn() };
    keyboard = { disableRotation: vi.fn() };
    project = vi.fn(() => ({ x: 0, y: 0 }));
  }
  const Map = MockMap as unknown as new (o: unknown) => MockMap;
  return {
    default: { Map, NavigationControl: class {} },
    Map,
    NavigationControl: class {},
  };
});
vi.mock("maplibre-gl/dist/maplibre-gl.css", () => ({}));

import { MapView } from "./Map";
import { getLayerCache, setLayerCache, LayerCache } from "./lib/layer_cache";

// --- test bus ------------------------------------------------------------ //
type SessionSubscriber = (p: unknown) => void;
function makeSessionBus() {
  const subs: SessionSubscriber[] = [];
  return {
    push: (p: unknown) => subs.forEach((s) => s(p)),
    subscribe: (cb: SessionSubscriber) => {
      subs.push(cb);
      return () => subs.splice(subs.indexOf(cb), 1);
    },
  };
}

function polygonFc(n: number) {
  return {
    type: "FeatureCollection",
    features: Array.from({ length: n }, (_, i) => ({
      type: "Feature",
      properties: { id: i },
      geometry: {
        type: "Polygon",
        coordinates: [[[i, 0], [i + 1, 0], [i + 1, 1], [i, 1], [i, 0]]],
      },
    })),
  };
}

function footprintLayer() {
  return {
    layer_id: "L_footprints",
    name: "Building footprints",
    layer_type: "vector",
    uri: "s3://x/footprints.fgb",
    style_preset: "ms_building_footprints",
    visible: true,
    opacity: 1,
    inline_geojson: polygonFc(50),
  };
}

function lightVectorLayer() {
  return {
    layer_id: "L_rivers",
    name: "Rivers",
    layer_type: "vector",
    uri: "s3://x/rivers.fgb",
    style_preset: "nhdplus_flowline",
    visible: true,
    opacity: 1,
    inline_geojson: {
      type: "FeatureCollection",
      features: [
        { type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: [[0, 0], [1, 1]] } },
      ],
    },
  };
}

describe("Map.tsx deck.gl interleaved-overlay routing (#169 spike)", () => {
  beforeEach(() => {
    overlaySetPropsCalls.length = 0;
    overlayInstances = 0;
    (globalThis as unknown as { __deckTestMap: unknown }).__deckTestMap = null;
    // Fresh cache with a stable active case so overrides + eviction resolve.
    setLayerCache(new LayerCache());
    getLayerCache().activeCaseId = "case-deck";
  });

  afterEach(() => {
    cleanup(); // unmount between tests so React isn't "already working".
  });

  // LAZY-LOAD (deck.gl SPIKE, #169): the overlay is NO LONGER constructed eagerly
  // on `load`. It is built on the FIRST deck-routed layer via a one-time
  // `await import("@deck.gl/mapbox")` + `await import("./lib/deck_layers")`
  // (both mocked here). Those dynamic imports resolve as microtasks, and
  // ensureDeckLoaded() then re-runs the reconcile, so the test must FLUSH the
  // microtask queue (inside act) before asserting on the overlay. A handful of
  // turns covers Promise.all -> the inner re-run -> setProps.
  async function flushDeck() {
    // The lazy `import("@deck.gl/mapbox")` + `import("./lib/deck_layers")` resolve
    // across several macro/micro turns (module eval of the real deck_layers + its
    // @deck.gl/layers dep). Pump timers + microtasks until the overlay has mounted
    // + emitted at least one setProps, or a bounded ceiling is hit.
    for (let i = 0; i < 50; i++) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => {
        await new Promise((r) => setTimeout(r, 0));
      });
      if (overlayInstances > 0 && overlaySetPropsCalls.length > 0) break;
    }
  }

  async function mountWithLayers(layers: unknown[]) {
    const bus = makeSessionBus();
    render(
      <MapView
        subscribeSessionState={bus.subscribe as never}
        caseActive
      />,
    );
    // Drive the map `load` (latches style-ready; the overlay is no longer built here).
    act(() => {
      getLastMap().fireOnce("load");
    });
    act(() => {
      bus.push({ loaded_layers: layers });
    });
    // Let the lazy deck.gl import resolve + the overlay mount + the rebuild re-run.
    await flushDeck();
    return bus;
  }

  it("LAZILY constructs ONE MapboxOverlay + addControl on the first footprint layer", async () => {
    // Before any deck-routed layer, the overlay must NOT exist (deck not loaded).
    const bus = makeSessionBus();
    render(<MapView subscribeSessionState={bus.subscribe as never} caseActive />);
    act(() => {
      getLastMap().fireOnce("load");
    });
    expect(overlayInstances).toBe(0); // lazy: nothing loaded yet on bare `load`.
    // A footprint layer triggers the one-time lazy load -> exactly one overlay.
    act(() => {
      bus.push({ loaded_layers: [footprintLayer()] });
    });
    await flushDeck();
    expect(overlayInstances).toBe(1);
    expect(getLastMap().addControl).toHaveBeenCalledTimes(1);
  });

  it("ROUTES a footprint inline-GeoJSON layer to the deck overlay (not MapLibre)", async () => {
    await mountWithLayers([footprintLayer()]);
    // Latest setProps carries the footprint as a deck layer.
    const last = overlaySetPropsCalls[overlaySetPropsCalls.length - 1];
    expect(last).toBeTruthy();
    expect(last!.layers.map((l) => l.id)).toContain("L_footprints");
    // It must NOT have created a MapLibre source (stayed off the MapLibre path).
    expect(getLastMap()._sources.has("L_footprints")).toBe(false);
  });

  it("does NOT deck-route a LIGHT vector (it stays on the MapLibre path)", async () => {
    await mountWithLayers([lightVectorLayer()]);
    // A light-only snapshot never triggers the lazy deck load, so no overlay exists.
    expect(overlayInstances).toBe(0);
    const last = overlaySetPropsCalls[overlaySetPropsCalls.length - 1];
    // No deck layer for the light vector.
    const ids = last ? last.layers.map((l) => l.id) : [];
    expect(ids).not.toContain("L_rivers");
  });

  it("mixes a footprint (deck) + a light vector (MapLibre) in one snapshot", async () => {
    await mountWithLayers([footprintLayer(), lightVectorLayer()]);
    const last = overlaySetPropsCalls[overlaySetPropsCalls.length - 1]!;
    const deckIds = last.layers.map((l) => l.id);
    expect(deckIds).toContain("L_footprints"); // heavy -> deck
    expect(deckIds).not.toContain("L_rivers"); // light -> MapLibre
    expect(getLastMap()._sources.has("L_footprints")).toBe(false); // not a MapLibre source
  });

  it("TRACKS the deck-routed id so an authoritative replace omitting it removes it", async () => {
    const bus = await mountWithLayers([footprintLayer()]);
    // Replace with an empty authoritative snapshot (Case switch / exit shape).
    act(() => {
      bus.push({ loaded_layers: [], replace_layers: true });
    });
    await flushDeck();
    const last = overlaySetPropsCalls[overlaySetPropsCalls.length - 1]!;
    expect(last.layers.map((l) => l.id)).not.toContain("L_footprints");
  });

  it("applies a layer_cache opacity override to the deck-routed layer", async () => {
    getLayerCache().setOverride("case-deck", "L_footprints", { opacity: 0.4 });
    await mountWithLayers([footprintLayer()]);
    const last = overlaySetPropsCalls[overlaySetPropsCalls.length - 1]!;
    const fp = last.layers.find((l) => l.id === "L_footprints") as
      | { id: string; props?: { opacity?: number } }
      | undefined;
    expect(fp).toBeTruthy();
    expect(fp!.props?.opacity).toBeCloseTo(0.4);
  });
});
