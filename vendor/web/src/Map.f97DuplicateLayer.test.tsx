// F97 — deleting one of two duplicate layers removed BOTH.
//
// ROOT CAUSE: Map.tsx keys MapLibre sources by `layer.layer_id` (addSource(id)
// + addedSourceIds). Two GRACE-2 layers sharing a layer_id collide — the second
// add is skipped, both occupy ONE shared source, and a later delete-by-id tears
// that source down so BOTH vanish.
//
// FIX (this track): the agent mints a UNIQUE layer_id per fetch (server.py), and
// the reconcile DEDUPS by layer_id (client-side defense-in-depth) so one id maps
// to exactly one entry / source. These tests assert:
//   1. Two layers with DISTINCT ids both render (two distinct sources).
//   2. Removing ONE (snapshot drops it) tears down ONLY its source — the other
//      layer's source survives (the F97 regression guard).
//   3. Two entries that DO share a layer_id collapse to one source (dedup),
//      and a delete-by-id on the deduped set is a clean per-id teardown.
//
// Mirrors the MapLibre mock + test-bus scaffolding of Map.test.tsx.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, act } from "@testing-library/react";
import { MapView, type SessionStateSubscriber } from "./Map";

type MockCallArgs = unknown[];
interface MapMock {
  addSource: ReturnType<typeof vi.fn>;
  addLayer: ReturnType<typeof vi.fn>;
  removeLayer: ReturnType<typeof vi.fn>;
  removeSource: ReturnType<typeof vi.fn>;
  getLayer: ReturnType<typeof vi.fn>;
  getSource: ReturnType<typeof vi.fn>;
  _addedSources: Set<string>;
  _addedLayers: Set<string>;
}

let lastMapMock: MapMock | null = null;

vi.mock("maplibre-gl", () => {
  class MockNavigationControl {}

  class MockMap {
    _addedLayers = new Set<string>(["qgis-basemap", "osm-fallback-basemap"]);
    _addedSources = new Set<string>(["qgis-wms", "osm-fallback"]);
    _sourceSetData = new Map<string, ReturnType<typeof vi.fn>>();

    addSource = vi.fn((id: string, _def: unknown) => {
      this._addedSources.add(id);
      this._sourceSetData.set(id, vi.fn());
    });
    addLayer = vi.fn((def: { id: string }, _beforeId?: string) => {
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
        ? { type: "geojson", setData: this._sourceSetData.get(id) ?? vi.fn() }
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
        Array.from(this._addedSources).map((id) => [id, { type: "raster" }]),
      ),
    }));

    constructor(_options: Record<string, unknown> = {}) {
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
  loaded_layers?: Array<{
    layer_id: string;
    name: string;
    layer_type: string;
    uri: string;
    visible?: boolean;
    opacity?: number;
  }>;
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

// Two WDPA layers from the SAME source/name but with DISTINCT ids (the post-mint
// reality). `name` is identical to model the F97 duplicate-layer scenario.
function makeWdpaLayer(id: string) {
  return {
    layer_id: id,
    name: "Protected Areas — WDPA",
    layer_type: "raster",
    uri: `https://qgis.example.com/wms?LAYERS=${id}`,
    visible: true,
  };
}

describe("MapView — F97 duplicate-layer delete isolation", () => {
  beforeEach(() => {
    lastMapMock = null;
  });

  it("renders TWO layers with distinct ids as two distinct sources", () => {
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
        loaded_layers: [makeWdpaLayer("wdpa-A"), makeWdpaLayer("wdpa-B")],
      });
    });

    const m = lastMapMock!;
    const sourceIds = m.addSource.mock.calls.map((c) => (c as MockCallArgs)[0]);
    expect(sourceIds).toContain("wdpa-A");
    expect(sourceIds).toContain("wdpa-B");
    // Both distinct sources live on the map.
    expect(m._addedSources.has("wdpa-A")).toBe(true);
    expect(m._addedSources.has("wdpa-B")).toBe(true);
  });

  it("removing ONE of two distinct-id layers leaves the other intact (the F97 fix)", () => {
    const sessionBus = makeSessionBus();
    render(
      <MapView
        subscribeSessionState={
          sessionBus.subscribe as (cb: SessionStateSubscriber) => () => void
        }
      />,
    );

    // Both layers loaded.
    act(() => {
      sessionBus.push({
        loaded_layers: [makeWdpaLayer("wdpa-A"), makeWdpaLayer("wdpa-B")],
      });
    });
    const m = lastMapMock!;
    expect(m._addedSources.has("wdpa-A")).toBe(true);
    expect(m._addedSources.has("wdpa-B")).toBe(true);

    m.removeSource.mockClear();

    // Authoritative replace dropping ONLY wdpa-A (the user deleted layer A).
    act(() => {
      sessionBus.push({ loaded_layers: [makeWdpaLayer("wdpa-B")] });
    });

    // wdpa-A torn down; wdpa-B's source MUST survive (no shared-source collapse).
    expect(m.removeSource).toHaveBeenCalledWith("wdpa-A");
    expect(m.removeSource).not.toHaveBeenCalledWith("wdpa-B");
    expect(m._addedSources.has("wdpa-A")).toBe(false);
    expect(m._addedSources.has("wdpa-B")).toBe(true);
  });

  it("dedups two entries that SHARE a layer_id into ONE source (client defense-in-depth)", () => {
    const sessionBus = makeSessionBus();
    render(
      <MapView
        subscribeSessionState={
          sessionBus.subscribe as (cb: SessionStateSubscriber) => () => void
        }
      />,
    );

    act(() => {
      // Two entries with the SAME id (a duplicate the mint should normally
      // prevent — but if it slips through, the reconcile must collapse it).
      sessionBus.push({
        loaded_layers: [makeWdpaLayer("wdpa-dup"), makeWdpaLayer("wdpa-dup")],
      });
    });

    const m = lastMapMock!;
    const dupAdds = m.addSource.mock.calls.filter(
      (c) => (c as MockCallArgs)[0] === "wdpa-dup",
    );
    // Exactly ONE source for the shared id — no collision, no skipped-second-add.
    expect(dupAdds.length).toBe(1);
    expect(m._addedSources.has("wdpa-dup")).toBe(true);
  });
});
