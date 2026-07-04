// GRACE-2 web — DrawController tests (FR-WC-16 urban vector-draw).
//
// Covers:
//   1. Draw-mode switching (rectangle / polygon / linestring / select).
//   2. Per-segment barrier tagging (wall / flap_gate + flap direction) — the
//      style/role round-trip.
//   3. Segment snip (removeFeatures).
//   4. Area-threshold discard of tiny polygons.
//   5. getFeatureCollection() — the RESPONSE-PAYLOAD-SHAPE test: a drawn AOI +
//      a wall + a flap gate read back as the role-tagged FeatureCollection that
//      satisfies the SpatialDrawFeatureCollection contract and feeds the SWMM
//      `barriers` kwarg unchanged.
//   6. polygonAreaM2() — the planar-metres area helper used by the discard.
//
// terra-draw needs a live MapLibre adapter, so we inject a lightweight in-memory
// TerraDraw stub via the DrawController's `makeDraw` dependency seam.

import { describe, it, expect, beforeEach } from "vitest";
import {
  DrawController,
  polygonAreaM2,
  type DrawFeatureId,
} from "./draw_controller";
import type { Map as MapLibreMap } from "maplibre-gl";
import type {
  GeoJSONStoreFeatures,
  TerraDraw,
} from "terra-draw";

// --- In-memory TerraDraw stub -------------------------------------------- //

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
  getSnapshotFeature(id: DrawFeatureId): GeoJSONStoreFeatures | undefined {
    const f = this.features.get(id);
    return f ? (JSON.parse(JSON.stringify(f)) as GeoJSONStoreFeatures) : undefined;
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
  _add(geometry: GeoJSONStoreFeatures["geometry"], properties: Record<string, unknown>): DrawFeatureId {
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

const fakeMap = {} as MapLibreMap;

function makeController(): { c: DrawController; fake: FakeTerraDraw } {
  const fake = new FakeTerraDraw();
  const c = new DrawController(fakeMap, {
    makeDraw: () => fake as unknown as TerraDraw,
  });
  c.start();
  return { c, fake };
}

const SQUARE: GeoJSONStoreFeatures["geometry"] = {
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

describe("DrawController — draw-mode + lifecycle", () => {
  it("starts terra-draw and switches modes", () => {
    const { c, fake } = makeController();
    expect(fake.started).toBe(true);
    c.setMode("rectangle");
    expect(c.getMode()).toBe("rectangle");
    c.setMode("linestring");
    expect(c.getMode()).toBe("linestring");
    c.setMode("polygon");
    expect(c.getMode()).toBe("polygon");
    c.setMode("select");
    expect(c.getMode()).toBe("select");
    c.stop();
    expect(fake.started).toBe(false);
  });

  it("fires onSelected only after a feature is selected", () => {
    const { c, fake } = makeController();
    const selected: DrawFeatureId[] = [];
    c.onSelected((id) => selected.push(id));
    const id = fake._add(lineGeom([[-85.30, 35.04], [-85.30, 35.05]]), {
      mode: "linestring",
    });
    fake._select(id);
    expect(selected).toEqual([id]);
    c.stop();
  });
});

describe("DrawController — barrier tagging", () => {
  let c: DrawController;
  let fake: FakeTerraDraw;
  beforeEach(() => {
    ({ c, fake } = makeController());
  });

  it("tags a wall (role=barrier, barrier_type=wall, no flap fields)", () => {
    const id = fake._add(lineGeom([[-85.305, 35.041], [-85.305, 35.048]]), {
      mode: "linestring",
    });
    c.tagBarrier(id, "wall");
    const f = fake.getSnapshotFeature(id)!;
    expect(f.properties.role).toBe("barrier");
    expect(f.properties.barrier_type).toBe("wall");
    expect(f.properties.flap_direction).toBeUndefined();
    expect(f.properties.protected_side).toBeUndefined();
  });

  it("tags a flap gate with a flap direction", () => {
    const id = fake._add(lineGeom([[-85.308, 35.043], [-85.302, 35.043]]), {
      mode: "linestring",
    });
    c.tagBarrier(id, "flap_gate", { flapDirection: "out", protectedSide: "left" });
    const f = fake.getSnapshotFeature(id)!;
    expect(f.properties.role).toBe("barrier");
    expect(f.properties.barrier_type).toBe("flap_gate");
    expect(f.properties.flap_direction).toBe("out");
    expect(f.properties.protected_side).toBe("left");
  });

  it("re-tagging a flap gate back to wall clears the flap fields", () => {
    const id = fake._add(lineGeom([[-85.308, 35.043], [-85.302, 35.043]]), {
      mode: "linestring",
    });
    c.tagBarrier(id, "flap_gate", { flapDirection: "in" });
    c.tagBarrier(id, "wall");
    const f = fake.getSnapshotFeature(id)!;
    expect(f.properties.barrier_type).toBe("wall");
    expect(f.properties.flap_direction).toBeUndefined();
  });
});

describe("DrawController — snip + clear", () => {
  it("snipFeature removes only the targeted segment", () => {
    const { c, fake } = makeController();
    const a = fake._add(lineGeom([[-85.31, 35.04], [-85.31, 35.05]]), { mode: "linestring" });
    const b = fake._add(lineGeom([[-85.30, 35.04], [-85.30, 35.05]]), { mode: "linestring" });
    c.snipFeature(a);
    const ids = fake.getSnapshot().map((f) => f.id);
    expect(ids).not.toContain(a);
    expect(ids).toContain(b);
    c.stop();
  });

  it("clear wipes all features", () => {
    const { c, fake } = makeController();
    fake._add(SQUARE, { mode: "polygon" });
    fake._add(lineGeom([[-85.31, 35.04], [-85.31, 35.05]]), { mode: "linestring" });
    c.clear();
    expect(fake.getSnapshot()).toHaveLength(0);
    c.stop();
  });
});

describe("DrawController — area-threshold discard", () => {
  it("drops polygons under the threshold, keeps larger ones + non-polygons", () => {
    const { c, fake } = makeController();
    // A ~123 m square (≈ 15,000 m²) — kept above a 1000 m² threshold.
    const big = fake._add(SQUARE, { mode: "polygon" });
    // A tiny ~1.1 m square (≈ 1.2 m²) — dropped.
    const tiny = fake._add(
      {
        type: "Polygon",
        coordinates: [
          [
            [-85.30000, 35.04000],
            [-85.29999, 35.04000],
            [-85.29999, 35.04001],
            [-85.30000, 35.04001],
            [-85.30000, 35.04000],
          ],
        ],
      },
      { mode: "polygon" },
    );
    const line = fake._add(lineGeom([[-85.31, 35.04], [-85.31, 35.05]]), { mode: "linestring" });

    const dropped = c.discardSmallPolygons(1000);
    expect(dropped).toEqual([tiny]);
    const ids = fake.getSnapshot().map((f) => f.id);
    expect(ids).toContain(big);
    expect(ids).toContain(line);
    expect(ids).not.toContain(tiny);
    c.stop();
  });
});

describe("DrawController — getFeatureCollection (RESPONSE-PAYLOAD SHAPE)", () => {
  it("reads back AOI + wall + flap gate as a role-tagged FeatureCollection", () => {
    const { c, fake } = makeController();
    // AOI polygon (terra-draw stamps `mode`; role inferred from geometry).
    fake._add(SQUARE, { mode: "polygon" });
    // A barrier wall.
    const wall = fake._add(lineGeom([[-85.305, 35.041], [-85.305, 35.048]]), {
      mode: "linestring",
    });
    c.tagBarrier(wall, "wall");
    // A barrier flap gate with direction.
    const flap = fake._add(lineGeom([[-85.308, 35.043], [-85.302, 35.043]]), {
      mode: "linestring",
    });
    c.tagBarrier(flap, "flap_gate", { flapDirection: "out", protectedSide: "left" });

    const fc = c.getFeatureCollection();
    expect(fc.type).toBe("FeatureCollection");
    expect(fc.features).toHaveLength(3);

    const roles = fc.features.map((f) => f.properties.role);
    expect(roles).toEqual(["aoi", "barrier", "barrier"]);

    // terra-draw's `mode` property must NOT leak onto the response.
    for (const f of fc.features) {
      expect((f.properties as Record<string, unknown>).mode).toBeUndefined();
    }

    const barriers = fc.features.filter((f) => f.properties.role === "barrier");
    expect(barriers.map((b) => b.properties.barrier_type)).toEqual([
      "wall",
      "flap_gate",
    ]);
    const flapFeature = barriers.find(
      (b) => b.properties.barrier_type === "flap_gate",
    )!;
    expect(flapFeature.properties.flap_direction).toBe("out");
    expect(flapFeature.properties.protected_side).toBe("left");
    // A wall carries no flap metadata.
    const wallFeature = barriers.find(
      (b) => b.properties.barrier_type === "wall",
    )!;
    expect(wallFeature.properties.flap_direction).toBeUndefined();

    // counts() reflects the inventory. (line: 0 in the default barrier mode --
    // LineStrings count as barriers, never neutral lines.)
    expect(c.counts()).toEqual({
      aoi: 1,
      barrier: 2,
      untaggedBarrier: 0,
      point: 0,
      line: 0,
    });
    c.stop();
  });

  it("an untagged barrier reads role=barrier with no barrier_type (honest, not coerced)", () => {
    const { c, fake } = makeController();
    fake._add(lineGeom([[-85.31, 35.04], [-85.31, 35.05]]), { mode: "linestring" });
    const fc = c.getFeatureCollection();
    expect(fc.features).toHaveLength(1);
    expect(fc.features[0]!.properties.role).toBe("barrier");
    expect(fc.features[0]!.properties.barrier_type).toBeUndefined();
    expect(c.counts().untaggedBarrier).toBe(1);
    c.stop();
  });
});

describe("DrawController -- neutral-line mode (purpose='line')", () => {
  function makeNeutralController(): { c: DrawController; fake: FakeTerraDraw } {
    const fake = new FakeTerraDraw();
    const c = new DrawController(fakeMap, {
      makeDraw: () => fake as unknown as TerraDraw,
      neutralLine: true,
    });
    c.start();
    return { c, fake };
  }

  it("reads an untagged LineString back as role='line' (NOT barrier)", () => {
    const { c, fake } = makeNeutralController();
    fake._add(lineGeom([[-85.31, 35.04], [-85.305, 35.045], [-85.30, 35.05]]), {
      mode: "linestring",
    });
    const fc = c.getFeatureCollection();
    expect(fc.features).toHaveLength(1);
    const feat = fc.features[0]!;
    expect(feat.properties.role).toBe("line");
    expect(feat.properties.barrier_type).toBeUndefined();
    expect(feat.geometry.type).toBe("LineString");
    c.stop();
  });

  it("counts a drawn line as `line`, never an untagged barrier", () => {
    const { c, fake } = makeNeutralController();
    fake._add(lineGeom([[-85.31, 35.04], [-85.30, 35.05]]), { mode: "linestring" });
    expect(c.counts()).toEqual({
      aoi: 0,
      barrier: 0,
      untaggedBarrier: 0,
      point: 0,
      line: 1,
    });
    c.stop();
  });
});

describe("polygonAreaM2", () => {
  it("computes an approximate area for a small square (within 5%)", () => {
    // The SQUARE spans ~0.01° lon × 0.01° lat near 35°N.
    // 0.01° lat ≈ 1113 m; 0.01° lon ≈ 1113·cos(35°) ≈ 911 m -> ≈ 1,014,000 m².
    const area = polygonAreaM2(SQUARE.coordinates as number[][][]);
    const expected = 1_014_000;
    expect(area).toBeGreaterThan(expected * 0.95);
    expect(area).toBeLessThan(expected * 1.05);
  });

  it("subtracts a hole ring from the outer ring", () => {
    const outer = [
      [-85.31, 35.04],
      [-85.30, 35.04],
      [-85.30, 35.05],
      [-85.31, 35.05],
      [-85.31, 35.04],
    ];
    const hole = [
      [-85.307, 35.043],
      [-85.303, 35.043],
      [-85.303, 35.047],
      [-85.307, 35.047],
      [-85.307, 35.043],
    ];
    const solid = polygonAreaM2([outer]);
    const withHole = polygonAreaM2([outer, hole]);
    expect(withHole).toBeLessThan(solid);
  });

  it("returns 0 for a degenerate ring", () => {
    expect(polygonAreaM2([[[0, 0], [1, 1]]])).toBe(0);
    expect(polygonAreaM2([])).toBe(0);
  });
});
