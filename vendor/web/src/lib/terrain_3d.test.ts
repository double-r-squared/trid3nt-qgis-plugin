// GRACE-2 web - lib/terrain_3d.ts unit tests ("3D terrain viz" first cut).
//
// Covers the PURE core:
//   - persistence helpers (3D + contour flags) default OFF, write-through.
//   - buildTerrainDemSource: TiTiler terrain-RGB primary vs AWS Terrarium
//     fallback, with the correct encoding per origin.
//   - applyTerrain3d / removeTerrain3d against a tiny structural Map stub:
//     adds the DEM source + hillshade + sky + setTerrain + unlocks pitch on
//     enable; tears it all down + re-locks 2D on remove; idempotent + defensive.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  LS_TERRAIN_3D,
  LS_CONTOURS,
  readTerrain3dEnabled,
  writeTerrain3dEnabled,
  readContoursEnabled,
  writeContoursEnabled,
  buildTerrainDemSource,
  applyTerrain3d,
  removeTerrain3d,
  startAoiPulseGlow,
  AWS_TERRAIN_TERRARIUM_TEMPLATE,
  TERRAIN_DEM_SOURCE_ID,
  TERRAIN_HILLSHADE_LAYER_ID,
  TERRAIN_SKY_LAYER_ID,
  TERRAIN_EXAGGERATION,
  TERRAIN_3D_PITCH,
  TERRAIN_3D_BEARING,
  FLAT_2D_PITCH,
  FLAT_2D_BEARING,
  buildTerrain3dCameraPose,
  buildFlat2dCameraPose,
  buildDrape3dResamplingExpression,
  TERRAIN_3D_CRISP_MIN_ZOOM,
  type TerrainMapLike,
  type TerrainDemSourceSpec,
  type PulseGlowMapLike,
} from "./terrain_3d";

describe("terrain_3d - persistence", () => {
  beforeEach(() => localStorage.clear());

  it("3D-terrain flag defaults OFF (absent value)", () => {
    expect(readTerrain3dEnabled()).toBe(false);
  });

  it("3D-terrain flag writes through and reads back ON", () => {
    writeTerrain3dEnabled(true);
    expect(localStorage.getItem(LS_TERRAIN_3D)).toBe("true");
    expect(readTerrain3dEnabled()).toBe(true);
    writeTerrain3dEnabled(false);
    expect(readTerrain3dEnabled()).toBe(false);
  });

  it("only the explicit string 'true' enables 3D (garbage reads OFF)", () => {
    localStorage.setItem(LS_TERRAIN_3D, "yes");
    expect(readTerrain3dEnabled()).toBe(false);
  });

  it("contour flag defaults OFF and writes through", () => {
    expect(readContoursEnabled()).toBe(false);
    writeContoursEnabled(true);
    expect(localStorage.getItem(LS_CONTOURS)).toBe("true");
    expect(readContoursEnabled()).toBe(true);
  });
});

describe("terrain_3d - buildTerrainDemSource", () => {
  it("falls back to AWS Terrarium when no DEM COG url is given", () => {
    const src = buildTerrainDemSource({ publicBase: "https://edge.example" });
    expect(src.origin).toBe("aws-terrarium");
    expect(src.type).toBe("raster-dem");
    expect(src.encoding).toBe("terrarium");
    expect(src.tiles[0]).toBe(AWS_TERRAIN_TERRARIUM_TEMPLATE);
  });

  it("falls back to AWS Terrarium when there is no public edge base", () => {
    const src = buildTerrainDemSource({
      publicBase: null,
      demCogUrl: "s3://bucket/dem.tif",
    });
    expect(src.origin).toBe("aws-terrarium");
    expect(src.encoding).toBe("terrarium");
  });

  it("builds a TiTiler terrain-RGB source when BOTH base + DEM COG present", () => {
    const src = buildTerrainDemSource({
      publicBase: "https://edge.example",
      demCogUrl: "s3://bucket/dem.tif",
    });
    expect(src.origin).toBe("titiler");
    expect(src.type).toBe("raster-dem");
    // Mapbox terrain-RGB encoding for the TiTiler path.
    expect(src.encoding).toBe("mapbox");
    const tpl = src.tiles[0];
    expect(tpl).toContain("https://edge.example/cog/tiles/{z}/{x}/{y}.png");
    // The DEM COG url is URL-encoded into ?url=.
    expect(tpl).toContain(`url=${encodeURIComponent("s3://bucket/dem.tif")}`);
    expect(tpl).toContain("colormap_name=terrainrgb");
  });
});

describe("terrain_3d - camera poses (Priority 1: make 3D look 3D)", () => {
  it("exaggeration reads as relief without going spiky (>= 1.2, < 1.8)", () => {
    // NATE 2026-06-26: 2.0x was too aggressive / spiky at the 67deg pitch over a
    // coarse global DEM; 1.4 keeps depth legible without exaggerated spikes.
    expect(TERRAIN_EXAGGERATION).toBeGreaterThanOrEqual(1.2);
    expect(TERRAIN_EXAGGERATION).toBeLessThan(1.8);
  });

  it("3D pose pitches strongly but stays under the 75deg max pitch", () => {
    const pose = buildTerrain3dCameraPose();
    expect(pose.pitch).toBe(TERRAIN_3D_PITCH);
    expect(pose.bearing).toBe(TERRAIN_3D_BEARING);
    // Strong relief read (>= 60) but under setMaxPitch(75) so easeTo is not clamped.
    expect(pose.pitch).toBeGreaterThanOrEqual(60);
    expect(pose.pitch).toBeLessThan(75);
    // A gentle off-axis bearing so ridges read as depth, not a head-on wall.
    expect(pose.bearing).toBeGreaterThan(0);
    expect(pose.bearing).toBeLessThanOrEqual(45);
  });

  it("flat pose is dead-on top-down, north-up", () => {
    const pose = buildFlat2dCameraPose();
    expect(pose).toEqual({ pitch: FLAT_2D_PITCH, bearing: FLAT_2D_BEARING });
    expect(pose.pitch).toBe(0);
    expect(pose.bearing).toBe(0);
  });
});

describe("terrain_3d - draped-raster resampling (3D crispness)", () => {
  it("crisp threshold sits in a sensible 'very far out' band", () => {
    // Moderate zoom-out (city/AOI z>=~10 down to ~z6) must stay crisp; only
    // continent-scale views soften. Keep the cut deep enough to be 'very far'.
    expect(TERRAIN_3D_CRISP_MIN_ZOOM).toBeGreaterThan(2);
    expect(TERRAIN_3D_CRISP_MIN_ZOOM).toBeLessThanOrEqual(8);
  });

  it("builds a zoom-step expr: linear below the threshold, nearest at/above", () => {
    const expr = buildDrape3dResamplingExpression();
    expect(expr).toEqual([
      "step",
      ["zoom"],
      "linear",
      TERRAIN_3D_CRISP_MIN_ZOOM,
      "nearest",
    ]);
    // MapLibre `step` semantics: output before the first stop, then the stop's
    // output at/above it. So < threshold => "linear" (soft), >= => "nearest".
    expect(expr[2]).toBe("linear");
    expect(expr[4]).toBe("nearest");
  });
});

// --- a tiny structural Map stub for the side-effect helpers -------------- //

function makeMapStub() {
  const sources = new Set<string>();
  const layers = new Set<string>();
  const m = {
    addSource: vi.fn((id: string) => sources.add(id)),
    removeSource: vi.fn((id: string) => sources.delete(id)),
    getSource: vi.fn((id: string) => (sources.has(id) ? {} : undefined)),
    addLayer: vi.fn((l: { id: string }) => layers.add(l.id)),
    removeLayer: vi.fn((id: string) => layers.delete(id)),
    getLayer: vi.fn((id: string) => (layers.has(id) ? {} : undefined)),
    setTerrain: vi.fn(),
    setMaxPitch: vi.fn(),
    dragRotate: { enable: vi.fn(), disable: vi.fn() },
    dragPan: { enable: vi.fn(), disable: vi.fn() },
    touchZoomRotate: { enableRotation: vi.fn(), disableRotation: vi.fn() },
    touchPitch: { enable: vi.fn(), disable: vi.fn() },
  };
  return { m: m as unknown as TerrainMapLike, raw: m, sources, layers };
}

describe("terrain_3d - applyTerrain3d", () => {
  it("adds DEM source + hillshade + sky, sets terrain, unlocks pitch/rotate", () => {
    const { m, raw, sources, layers } = makeMapStub();
    const origin = applyTerrain3d(m);
    expect(origin).toBe("aws-terrarium"); // no public base / DEM COG in test env

    expect(sources.has(TERRAIN_DEM_SOURCE_ID)).toBe(true);
    expect(layers.has(TERRAIN_HILLSHADE_LAYER_ID)).toBe(true);
    expect(layers.has(TERRAIN_SKY_LAYER_ID)).toBe(true);

    expect(raw.setTerrain).toHaveBeenCalledWith({
      source: TERRAIN_DEM_SOURCE_ID,
      exaggeration: TERRAIN_EXAGGERATION,
    });
    // Camera unlocked for 3D.
    expect(raw.setMaxPitch).toHaveBeenCalledWith(75);
    // Left-drag PAN explicitly re-enabled so 3D is navigable even if a prior
    // draw gesture left dragPan disabled (the "can't pan in 3D" fix).
    expect(raw.dragPan.enable).toHaveBeenCalled();
    expect(raw.dragRotate.enable).toHaveBeenCalled();
    expect(raw.touchZoomRotate.enableRotation).toHaveBeenCalled();
    expect(raw.touchPitch.enable).toHaveBeenCalled();
  });

  it("is idempotent - a second apply does not re-add the source/layers", () => {
    const { m, raw } = makeMapStub();
    applyTerrain3d(m);
    applyTerrain3d(m);
    expect(raw.addSource).toHaveBeenCalledTimes(1);
    // hillshade + sky added once each.
    expect(raw.addLayer).toHaveBeenCalledTimes(2);
  });

  it("honors a supplied TiTiler DEM source (origin reported)", () => {
    const { m } = makeMapStub();
    const titiler: TerrainDemSourceSpec = {
      type: "raster-dem",
      tiles: ["https://edge/cog/tiles/{z}/{x}/{y}.png?url=x&colormap_name=terrainrgb"],
      tileSize: 256,
      encoding: "mapbox",
      maxzoom: 18,
      attribution: "x",
      origin: "titiler",
    };
    expect(applyTerrain3d(m, { demSource: titiler })).toBe("titiler");
  });

  it("logs a TODO (does not throw) when contours are requested", () => {
    const { m } = makeMapStub();
    const info = vi.spyOn(console, "info").mockImplementation(() => {});
    expect(() => applyTerrain3d(m, { contoursRequested: true })).not.toThrow();
    expect(info).toHaveBeenCalled();
    expect(String(info.mock.calls[0]?.[0])).toContain("maplibre-contour");
    info.mockRestore();
  });
});

describe("terrain_3d - removeTerrain3d", () => {
  it("setTerrain(null), drops layers + source, re-locks 2D camera", () => {
    const { m, raw, sources, layers } = makeMapStub();
    applyTerrain3d(m);
    raw.setMaxPitch.mockClear();

    removeTerrain3d(m);
    expect(raw.setTerrain).toHaveBeenLastCalledWith(null);
    expect(sources.has(TERRAIN_DEM_SOURCE_ID)).toBe(false);
    expect(layers.has(TERRAIN_HILLSHADE_LAYER_ID)).toBe(false);
    expect(layers.has(TERRAIN_SKY_LAYER_ID)).toBe(false);
    // Re-locked to flat 2D.
    expect(raw.setMaxPitch).toHaveBeenCalledWith(0);
    expect(raw.dragRotate.disable).toHaveBeenCalled();
    expect(raw.touchZoomRotate.disableRotation).toHaveBeenCalled();
    expect(raw.touchPitch.disable).toHaveBeenCalled();
    // Pan stays available in 2D - removing 3D must NOT disable dragPan (the flat
    // base map is pan+zoom). Only rotate/pitch re-lock.
    expect(raw.dragPan.disable).not.toHaveBeenCalled();
  });

  it("is safe to call when terrain was never enabled (no throw)", () => {
    const { m, raw } = makeMapStub();
    expect(() => removeTerrain3d(m)).not.toThrow();
    expect(raw.setTerrain).toHaveBeenCalledWith(null);
  });
});

// --- ISSUE 1 (NATE 2026-06-24): the 3D AOI pulse-glow must NOT scale the box -//
//
// The glow used to sine-animate `line-width` (1.5 <-> 3.5), which under a pitched
// 3D camera reads as the dashed AOI box GROWING and SHRINKING ("gets large and
// small ... hard to see"). The fix keeps geometry size CONSTANT: only
// line-opacity + line-blur animate. These tests drive the rAF loop by capturing
// the tick callback and invoking it at several timestamps, then assert NO
// line-width change off the constant ever happens.

describe("startAoiPulseGlow - GEOMETRY-STABLE (no line-width animation)", () => {
  const LAYER_ID = "grace2-analysis-extent-line";

  // A stub map that records every setPaintProperty(name -> values[]) call.
  function makeGlowMap() {
    const calls: Record<string, unknown[]> = {};
    const m: PulseGlowMapLike = {
      getLayer: () => ({}), // layer always present.
      setPaintProperty: (_layerId: string, name: string, value: unknown) => {
        (calls[name] ??= []).push(value);
      },
    };
    return { m, calls };
  }

  // Capture rAF callbacks so the test can step the loop deterministically.
  let rafCb: FrameRequestCallback | null;
  beforeEach(() => {
    rafCb = null;
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      rafCb = cb;
      return 1;
    });
    vi.stubGlobal("cancelAnimationFrame", () => {
      rafCb = null;
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function step(now: number): void {
    const cb = rafCb;
    rafCb = null; // the loop re-registers via rAF, capturing the next cb.
    cb?.(now);
  }

  it("NEVER sets line-width to anything but the constant 1.5 across the cycle", () => {
    const { m, calls } = makeGlowMap();
    const handle = startAoiPulseGlow(m, LAYER_ID);
    // Drive a full sine cycle (trough -> peak -> trough) at several phases.
    [0, 200, 400, 800, 1200, 1600].forEach(step);
    handle.stop();

    // Every line-width value ever set is exactly the static constant 1.5 - the
    // box size never changes (this is the whole Issue-1 fix).
    const widths = calls["line-width"] ?? [];
    expect(widths.length).toBeGreaterThan(0); // it does assert the constant.
    for (const w of widths) {
      expect(w).toBe(1.5);
    }
  });

  it("DOES animate line-opacity + line-blur (the glow still reads)", () => {
    const { m, calls } = makeGlowMap();
    const handle = startAoiPulseGlow(m, LAYER_ID);
    [0, 400, 800].forEach(step);
    handle.stop();

    const opacities = (calls["line-opacity"] ?? []) as number[];
    const blurs = (calls["line-blur"] ?? []) as number[];
    // Opacity + blur both got animated values during the loop.
    expect(opacities.length).toBeGreaterThanOrEqual(2);
    expect(blurs.length).toBeGreaterThanOrEqual(2);
    // The opacity actually varies (not a single constant) -> a real pulse.
    const distinctOpacities = new Set(opacities.map((v) => v.toFixed(3)));
    expect(distinctOpacities.size).toBeGreaterThan(1);
    // Opacity stays within the [0.55, 1.0] glow band.
    for (const o of opacities) {
      expect(o).toBeGreaterThanOrEqual(0.55 - 1e-9);
      expect(o).toBeLessThanOrEqual(1.0 + 1e-9);
    }
  });

  it("stop() restores the static line paint (width 1.5, opacity 0.9, blur 0)", () => {
    const { m, calls } = makeGlowMap();
    const handle = startAoiPulseGlow(m, LAYER_ID);
    [0, 400].forEach(step);
    handle.stop();
    // The last write of each property after stop() is the static restore.
    const widths = (calls["line-width"] ?? []) as number[];
    const opacities = (calls["line-opacity"] ?? []) as number[];
    const blurs = (calls["line-blur"] ?? []) as number[];
    expect(widths.at(-1)).toBe(1.5);
    expect(opacities.at(-1)).toBe(0.9);
    expect(blurs.at(-1)).toBe(0);
  });

  it("stop() is idempotent and start is a safe no-op without rAF", () => {
    const { m } = makeGlowMap();
    const handle = startAoiPulseGlow(m, LAYER_ID);
    expect(() => {
      handle.stop();
      handle.stop();
    }).not.toThrow();
  });
});
