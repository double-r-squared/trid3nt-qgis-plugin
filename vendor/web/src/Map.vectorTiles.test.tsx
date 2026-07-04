// F94 — dense vector layers render as a MapLibre VECTOR-TILE source so the
// browser only draws what is in view (instead of one giant inline-GeoJSON
// FeatureCollection that made the app laggy on OSM building footprints).
//
// This suite drives MapView's session-state apply loop with a wire layer that
// carries `vector_tile_url` and asserts that Map.tsx adds a `type: "vector"`
// source + the geometry-appropriate paint layer(s). It mirrors the maplibre-gl
// mock used by Map.test.tsx so the synchronous apply path is what we verify.

import { describe, it, expect, beforeEach } from "vitest";
import { render, act } from "@testing-library/react";
import { MapView, type SessionStateSubscriber } from "./Map";

type MockCallArgs = unknown[];

interface MapMock {
  addSource: ReturnType<typeof import("vitest").vi.fn>;
  addLayer: ReturnType<typeof import("vitest").vi.fn>;
}

import { vi } from "vitest";

let lastMapMock: MapMock | null = null;

vi.mock("maplibre-gl", () => {
  class MockNavigationControl {}

  class MockMap {
    _addedLayers = new Set<string>(["qgis-basemap", "osm-fallback-basemap"]);
    _addedSources = new Set<string>(["qgis-wms", "osm-fallback"]);
    _sourceSetData = new Map<string, ReturnType<typeof vi.fn>>();

    addSource = vi.fn((id: string) => {
      this._addedSources.add(id);
      this._sourceSetData.set(id, vi.fn());
    });
    addLayer = vi.fn((def: { id: string }) => {
      this._addedLayers.add(def.id);
    });
    removeLayer = vi.fn((id: string) => {
      this._addedLayers.delete(id);
    });
    removeSource = vi.fn((id: string) => {
      this._addedSources.delete(id);
    });
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
        ? { type: "vector", setData: this._sourceSetData.get(id) ?? vi.fn() }
        : null,
    );
    remove = vi.fn();
    on = vi.fn();
    off = vi.fn();
    once = vi.fn();
    project = vi.fn((lngLat: [number, number]) => ({
      x: (lngLat[0] + 180) * 2,
      y: (90 - lngLat[1]) * 2,
    }));
    getCanvas = vi.fn(() => ({ clientWidth: 1024, clientHeight: 768 }));
    getStyle = vi.fn(() => ({
      layers: Array.from(this._addedLayers).map((id) => ({ id })),
      sources: Object.fromEntries(
        Array.from(this._addedSources).map((id) => [id, { type: "vector" }]),
      ),
    }));

    constructor() {
      lastMapMock = this as unknown as MapMock;
    }
  }

  return {
    default: { Map: MockMap, NavigationControl: MockNavigationControl },
    Map: MockMap,
    NavigationControl: MockNavigationControl,
  };
});

vi.mock("maplibre-gl/dist/maplibre-gl.css", () => ({}));

interface WireSessionState {
  loaded_layers?: Array<Record<string, unknown>>;
}

type SessionSubscriber = (p: WireSessionState) => void;

function makeSessionBus() {
  const subs: SessionSubscriber[] = [];
  const push = (p: WireSessionState) => subs.forEach((s) => s(p));
  const subscribe = (cb: SessionSubscriber) => {
    subs.push(cb);
    return () => {
      subs.splice(subs.indexOf(cb), 1);
    };
  };
  return { push, subscribe };
}

describe("MapView — F94 dense vector-tile source wiring", () => {
  beforeEach(() => {
    lastMapMock = null;
  });

  it("adds a `vector` source (not geojson) for a layer carrying vector_tile_url", () => {
    const sessionBus = makeSessionBus();
    render(
      <MapView
        subscribeSessionState={
          sessionBus.subscribe as (cb: SessionStateSubscriber) => () => void
        }
      />,
    );

    act(() => {
      sessionBus.push({
        loaded_layers: [
          {
            layer_id: "osm-buildings",
            name: "OSM buildings",
            layer_type: "vector",
            uri: "s3://b/osm_buildings.pmtiles",
            visible: true,
            style_preset: "osm_buildings",
            vector_tile_url:
              "https://tiles.example/osm-buildings/{z}/{x}/{y}.pbf",
            vector_geom_kind: "polygon",
            vector_source_layer: "vector",
          },
        ],
      });
    });

    const m = lastMapMock!;
    expect(m.addSource).toHaveBeenCalledOnce();
    const [sourceId, sourceDef] = m.addSource.mock.calls[0] as MockCallArgs;
    expect(sourceId).toBe("osm-buildings");
    expect((sourceDef as { type: string }).type).toBe("vector");
    // Plain {z}/{x}/{y} template => `tiles` array, NOT inline geojson `data`.
    expect((sourceDef as { tiles?: string[] }).tiles?.[0]).toContain(
      "{z}/{x}/{y}.pbf",
    );
    expect((sourceDef as { data?: unknown }).data).toBeUndefined();
  });

  it("adds a polygon fill + outline layer addressing the MVT source-layer", () => {
    const sessionBus = makeSessionBus();
    render(
      <MapView
        subscribeSessionState={
          sessionBus.subscribe as (cb: SessionStateSubscriber) => () => void
        }
      />,
    );

    act(() => {
      sessionBus.push({
        loaded_layers: [
          {
            layer_id: "osm-buildings",
            name: "OSM buildings",
            layer_type: "vector",
            uri: "s3://b/osm_buildings.pmtiles",
            visible: true,
            style_preset: "osm_buildings",
            vector_tile_url:
              "https://tiles.example/osm-buildings/{z}/{x}/{y}.pbf",
            vector_geom_kind: "polygon",
            vector_source_layer: "buildings",
          },
        ],
      });
    });

    const m = lastMapMock!;
    const layerDefs = m.addLayer.mock.calls.map(
      (c) => c[0] as Record<string, unknown>,
    );
    const fill = layerDefs.find((d) => d.id === "osm-buildings");
    const outline = layerDefs.find((d) => d.id === "osm-buildings-outline");
    expect(fill).toBeDefined();
    expect(fill!.type).toBe("fill");
    expect(fill!.source).toBe("osm-buildings");
    // The vector-tile path MUST address the MVT source-layer (a geojson source
    // never sets this) — proves it is the tiled branch, not the inline branch.
    expect(fill!["source-layer"]).toBe("buildings");
    expect(outline).toBeDefined();
    expect(outline!.type).toBe("line");
    expect(outline!["source-layer"]).toBe("buildings");
  });

  it("uses a pmtiles `url` field for pmtiles:// vector_tile_url", () => {
    const sessionBus = makeSessionBus();
    render(
      <MapView
        subscribeSessionState={
          sessionBus.subscribe as (cb: SessionStateSubscriber) => () => void
        }
      />,
    );

    act(() => {
      sessionBus.push({
        loaded_layers: [
          {
            layer_id: "footprints",
            name: "Footprints",
            layer_type: "vector",
            uri: "s3://b/footprints.pmtiles",
            visible: true,
            style_preset: "osm_buildings",
            vector_tile_url: "pmtiles://https://cdn.example/footprints.pmtiles",
            vector_geom_kind: "polygon",
          },
        ],
      });
    });

    const m = lastMapMock!;
    const [, sourceDef] = m.addSource.mock.calls[0] as MockCallArgs;
    expect((sourceDef as { type: string }).type).toBe("vector");
    expect((sourceDef as { url?: string }).url).toBe(
      "pmtiles://https://cdn.example/footprints.pmtiles",
    );
    expect((sourceDef as { tiles?: unknown }).tiles).toBeUndefined();
  });
});
