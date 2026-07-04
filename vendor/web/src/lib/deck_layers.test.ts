// GRACE-2 web - deck_layers.ts unit tests (deck.gl SPIKE, #169).
//
// happy-dom cannot run WebGL, so we DO NOT construct a live GeoJsonLayer-on-canvas
// here. Instead we unit-test the PURE seams that decide the spike's behavior:
//   - the routing predicate (heavy/footprint -> deck; light vector -> MapLibre;
//     tiled / non-inline -> never deck)
//   - the layer_cache override application (opacity / visibility / zIndex)
//   - outline-only color + styling props
//   - the layer id (so Map.tsx can track + remove deck-routed layers)
// buildDeckGeoJsonLayer IS exercised (a GeoJsonLayer constructs fine without a
// GL context - only rendering needs one) to assert the outline-only props +
// pickable-suppression flow.

import { describe, it, expect } from "vitest";
import type { FeatureCollection } from "geojson";
import {
  shouldRouteToDeck,
  isFootprintLayer,
  resolveDeckLayerProps,
  buildDeckGeoJsonLayer,
  featureCount,
  hexToRgb,
  DECK_FEATURE_COUNT_THRESHOLD,
  DECK_POLYGON_FILL_ALPHA,
  DECK_LINE_WIDTH_PX,
  type DeckRoutableLayer,
} from "./deck_layers";
import type { LayerViewOverride } from "./layer_cache";

// --- fixtures -------------------------------------------------------------- //

/** A FeatureCollection of `n` unit-square polygons (footprint-shaped). */
function polygonFc(n: number): FeatureCollection {
  const features = Array.from({ length: n }, (_, i) => ({
    type: "Feature" as const,
    properties: { id: i, name: `bldg-${i}` },
    geometry: {
      type: "Polygon" as const,
      coordinates: [
        [
          [i, 0],
          [i + 1, 0],
          [i + 1, 1],
          [i, 1],
          [i, 0],
        ],
      ],
    },
  }));
  return { type: "FeatureCollection", features };
}

function footprintLayer(n: number): DeckRoutableLayer {
  return {
    layer_id: "L_footprints",
    name: "Building footprints",
    layer_type: "vector",
    style_preset: "ms_building_footprints",
    inline_geojson: polygonFc(n),
  };
}

function lightVectorLayer(n: number): DeckRoutableLayer {
  return {
    layer_id: "L_rivers",
    name: "NHDPlus flowlines",
    layer_type: "vector",
    style_preset: "nhdplus_flowline",
    inline_geojson: {
      type: "FeatureCollection",
      features: Array.from({ length: n }, (_, i) => ({
        type: "Feature" as const,
        properties: { id: i },
        geometry: {
          type: "LineString" as const,
          coordinates: [
            [i, 0],
            [i + 1, 1],
          ],
        },
      })),
    },
  };
}

// --- routing predicate ----------------------------------------------------- //

describe("shouldRouteToDeck (the routing predicate)", () => {
  it("routes a FOOTPRINT layer to deck even when small (intent-first)", () => {
    expect(shouldRouteToDeck(footprintLayer(10))).toBe(true);
  });

  it("routes a HEAVY non-footprint inline vector (> threshold) to deck", () => {
    const heavy: DeckRoutableLayer = {
      layer_id: "L_dense",
      name: "Parcels",
      style_preset: "parcels",
      inline_geojson: polygonFc(DECK_FEATURE_COUNT_THRESHOLD + 1),
    };
    expect(shouldRouteToDeck(heavy)).toBe(true);
  });

  it("does NOT route a LIGHT vector (stays on MapLibre)", () => {
    const light = lightVectorLayer(50);
    expect(shouldRouteToDeck(light)).toBe(false);
  });

  it("does NOT route a non-footprint layer exactly AT the threshold", () => {
    const atThreshold: DeckRoutableLayer = {
      layer_id: "L_at",
      name: "Stuff",
      inline_geojson: polygonFc(DECK_FEATURE_COUNT_THRESHOLD),
    };
    expect(shouldRouteToDeck(atThreshold)).toBe(false);
  });

  it("does NOT route a vector-tile (MVT) layer - that is MapLibre's job", () => {
    const tiled: DeckRoutableLayer = {
      layer_id: "L_mvt",
      name: "Building footprints",
      style_preset: "ms_building_footprints",
      vector_tile_url: "https://tiles.example/{z}/{x}/{y}.pbf",
      // even with inline present, a tiled layer is never deck-routed:
      inline_geojson: polygonFc(5000),
    };
    expect(shouldRouteToDeck(tiled)).toBe(false);
  });

  it("does NOT route a layer with no inline_geojson", () => {
    const noInline: DeckRoutableLayer = {
      layer_id: "L_uri",
      name: "Building footprints",
      style_preset: "ms_building_footprints",
    };
    expect(shouldRouteToDeck(noInline)).toBe(false);
  });

  it("does NOT route a malformed inline payload (not a FeatureCollection)", () => {
    const bad: DeckRoutableLayer = {
      layer_id: "L_bad",
      name: "Building footprints",
      style_preset: "ms_building_footprints",
      inline_geojson: { type: "NotACollection" },
    };
    expect(shouldRouteToDeck(bad)).toBe(false);
  });
});

describe("isFootprintLayer", () => {
  it("matches by style_preset and by name, case-insensitively", () => {
    expect(isFootprintLayer({ layer_id: "a", style_preset: "MS_Building_Footprints" })).toBe(true);
    expect(isFootprintLayer({ layer_id: "b", name: "OSM Building footprints" })).toBe(true);
    expect(isFootprintLayer({ layer_id: "c", name: "Structure footprints" })).toBe(true);
    expect(isFootprintLayer({ layer_id: "d", name: "Rivers", style_preset: "nhdplus" })).toBe(false);
  });
});

describe("featureCount", () => {
  it("returns the count for a FeatureCollection and null otherwise", () => {
    expect(featureCount(polygonFc(3))).toBe(3);
    expect(featureCount({ type: "Feature" })).toBeNull();
    expect(featureCount(null)).toBeNull();
    expect(featureCount("nope")).toBeNull();
  });
});

describe("hexToRgb", () => {
  it("parses #rrggbb and falls back to slate on garbage", () => {
    expect(hexToRgb("#708090")).toEqual([112, 128, 144]);
    expect(hexToRgb("FF7F0E")).toEqual([255, 127, 14]);
    expect(hexToRgb("not-a-color")).toEqual([112, 128, 144]);
  });
});

// --- override application + outline styling -------------------------------- //

describe("resolveDeckLayerProps (layer_cache overrides + outline styling)", () => {
  it("defaults to visible:true, opacity:1, zIndex:0 when no override", () => {
    const p = resolveDeckLayerProps(footprintLayer(10), undefined);
    expect(p.visible).toBe(true);
    expect(p.opacity).toBe(1);
    expect(p.zIndex).toBe(0);
  });

  it("applies the layer_cache opacity + visibility + zIndex override", () => {
    const ov: LayerViewOverride = { opacity: 0.35, visible: false, zIndex: 7 };
    const p = resolveDeckLayerProps(footprintLayer(10), ov);
    expect(p.opacity).toBeCloseTo(0.35);
    expect(p.visible).toBe(false);
    expect(p.zIndex).toBe(7);
  });

  it("clamps an out-of-range override opacity to [0,1]", () => {
    expect(resolveDeckLayerProps(footprintLayer(10), { opacity: 2 }).opacity).toBe(1);
    expect(resolveDeckLayerProps(footprintLayer(10), { opacity: -1 }).opacity).toBe(0);
  });

  it("is OUTLINE-ONLY: stroked, a FAINT fill alpha (interior pickable), thin line", () => {
    const p = resolveDeckLayerProps(footprintLayer(10), undefined);
    expect(p.stroked).toBe(true);
    expect(p.filled).toBe(true);
    // The interior fill is barely visible (faint alpha) so the OUTLINE reads.
    expect(p.fillColor[3]).toBe(DECK_POLYGON_FILL_ALPHA);
    expect(DECK_POLYGON_FILL_ALPHA).toBeLessThan(32);
    expect(p.lineWidthPx).toBe(DECK_LINE_WIDTH_PX);
    // line + fill share the resolved hue (slate for an unknown polygon preset).
    expect(p.lineColor).toEqual([p.fillColor[0], p.fillColor[1], p.fillColor[2]]);
  });

  it("carries the layer_id so Map.tsx can track + remove the deck-routed layer", () => {
    const p = resolveDeckLayerProps(footprintLayer(10), undefined);
    expect(p.id).toBe("L_footprints");
  });
});

// --- the built GeoJsonLayer instance --------------------------------------- //

describe("buildDeckGeoJsonLayer", () => {
  it("builds a GeoJsonLayer with the layer id + outline-only props", () => {
    const l = buildDeckGeoJsonLayer(footprintLayer(10), { opacity: 0.5 });
    expect(l.id).toBe("L_footprints");
    expect(l.props.stroked).toBe(true);
    expect(l.props.filled).toBe(true);
    expect(l.props.opacity).toBeCloseTo(0.5);
    expect(l.props.pickable).toBe(true);
    expect(l.props.lineWidthUnits).toBe("pixels");
  });

  it("forces pickable:false when picking is suppressed (draw/pick in flight)", () => {
    const l = buildDeckGeoJsonLayer(footprintLayer(10), undefined, {
      suppressPicking: true,
    });
    expect(l.props.pickable).toBe(false);
  });

  it("honors a hidden override (visible:false) on the built layer", () => {
    const l = buildDeckGeoJsonLayer(footprintLayer(10), { visible: false });
    expect(l.props.visible).toBe(false);
  });

  it("wires the onClick handler through for the picking bridge", () => {
    const onClick = () => {};
    const l = buildDeckGeoJsonLayer(footprintLayer(10), undefined, { onClick });
    expect(l.props.onClick).toBe(onClick);
  });
});
